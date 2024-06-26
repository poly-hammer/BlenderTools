# Copyright Epic Games, Inc. All Rights Reserved.


# ---------- edit metarig mode viewport settings ----------

metarig_mode_settings = {
    'source_rig': {
        'selected': False,
        'hidden': False,
        'mode': 'OBJECT',
        'pose_position': 'REST',
        'display_type': 'WIRE',
        'show_names': True,
        'show_in_front': True,
        'use_mirror_x': True,
        'use_snap': True,
        'snap_elements': {'VERTEX'},
        'hide_rig_mesh': True,
        'relationship_lines': True,
        'red_sphere_bones': False
    },
    'metarig': {
        'selected': True,
        'hidden': False,
        'mode': 'EDIT',
        'pose_position': 'REST',
        'display_type': 'TEXTURED',
        'show_names': True,
        'show_in_front': True,
        'use_mirror_x': True,
        'use_snap': True,
        'snap_elements': {'VERTEX'},
        'hide_rig_mesh': False,
        'relationship_lines': True,
        'red_sphere_bones': False
    }

}

# ---------- edit fk to source mode viewport settings ----------

fk_to_source_mode_settings = {
    'source_rig': {
        'selected': True,
        'hidden': False,
        'mode': 'POSE',
        'pose_position': 'POSE',
        'display_type': 'TEXTURED',
        'show_names': True,
        'show_in_front': True,
        'use_mirror_x': False,
        'use_snap': False,
        'snap_elements': {'VERTEX'},
        'hide_rig_mesh': False,
        'relationship_lines': False,
        'red_sphere_bones': True
    },
    'control_rig': {
        'selected': True,
        'hidden': False,
        'mode': 'POSE',
        'pose_position': 'POSE',
        'display_type': 'TEXTURED',
        'show_names': True,
        'show_in_front': True,
        'use_mirror_x': False,
        'use_snap': False,
        'snap_elements': {'VERTEX'},
        'hide_rig_mesh': False,
        'red_sphere_bones': False,
        'relationship_lines': False,
        'visible_bone_layers': [28, 8, 9, 12, 11, 15, 18, 6, 5, 3, 4, 14, 17],
        'visible_bone_collections': ["Root", "Arm.L (FK)", "Arm.L (Tweak)", 
                                     "Arm.R (Tweak)", "Arm.R (FK)", "Leg.L (Tweak)", 
                                     "Leg.R (Tweak)", "Fingers (Detail)", "Fingers", 
                                     "Torso", "Torso (Tweak)", "Leg.L (FK)", "Leg.R (FK)"]
    }}

# ---------- edit source to deform mode viewport settings ----------

source_to_deform_mode_settings = {
    'source_rig': {
        'selected': True,
        'hidden': False,
        'mode': 'POSE',
        'pose_position': 'POSE',
        'display_type': 'TEXTURED',
        'show_names': True,
        'show_in_front': True,
        'use_mirror_x': False,
        'use_snap': False,
        'snap_elements': {'VERTEX'},
        'hide_rig_mesh': False,
        'relationship_lines': False,
        'red_sphere_bones': True
    },
    'control_rig': {
        'selected': True,
        'hidden': False,
        'mode': 'POSE',
        'pose_position': 'REST',
        'display_type': 'TEXTURED',
        'show_names': True,
        'show_in_front': True,
        'use_mirror_x': False,
        'use_snap': False,
        'snap_elements': {'VERTEX'},
        'hide_rig_mesh': False,
        'relationship_lines': False,
        'visible_bone_layers': [29],
        'visible_bone_collections': ["Root" ,"DEF"],
        'red_sphere_bones': False
    }
}

# ---------- control mode viewport settings ----------

control_mode_settings = {
    'source_rig': {
        'selected': False,
        'hidden': True,
        'mode': 'OBJECT',
        'pose_position': 'POSE',
        'display_type': 'TEXTURED',
        'show_names': False,
        'show_in_front': False,
        'use_mirror_x': False,
        'use_snap': False,
        'snap_elements': {'VERTEX'},
        'hide_rig_mesh': False,
        'relationship_lines': False,
        'red_sphere_bones': False
    },
    'control_rig': {
        'selected': True,
        'hidden': False,
        'mode': 'POSE',
        'pose_position': 'POSE',
        'display_type': 'TEXTURED',
        'show_names': False,
        'show_in_front': False,
        'use_mirror_x': False,
        'use_snap': False,
        'snap_elements': {'VERTEX'},
        'hide_rig_mesh': False,
        'relationship_lines': False,
        'red_sphere_bones': False
    }
}
