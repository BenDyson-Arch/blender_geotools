"""Build profiles: add, remove and reorder. Publish (publish.py) uses the active one."""

import bpy
from bpy.props import EnumProperty

from .common import redraw_ui


def _scene_poll(context: bpy.types.Context) -> bool:
    return context.scene is not None


def _active_profile_poll(cls: type, context: bpy.types.Context) -> bool:
    if context.scene is None:
        return False
    props = context.scene.geotools
    if not 0 <= props.build_profile_index < len(props.build_profiles):
        cls.poll_message_set("No build profile selected")
        return False
    return True


def unique_profile_name(profiles: bpy.types.bpy_prop_collection, base: str = "Profile") -> str:
    names = {p.name for p in profiles}
    n = len(profiles) + 1
    while f"{base} {n}" in names:
        n += 1
    return f"{base} {n}"


class GEOTOOLS_OT_build_profile_add(bpy.types.Operator):
    """Add a build profile"""

    bl_idname = "geotools.build_profile_add"
    bl_label = "Add Build Profile"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _scene_poll(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = context.scene.geotools
        name = unique_profile_name(props.build_profiles)
        profile = props.build_profiles.add()
        profile.name = name
        props.build_profile_index = len(props.build_profiles) - 1
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_build_profile_remove(bpy.types.Operator):
    """Remove the selected build profile"""

    bl_idname = "geotools.build_profile_remove"
    bl_label = "Remove Build Profile"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_profile_poll(cls, context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = context.scene.geotools
        index = props.build_profile_index
        props.build_profiles.remove(index)
        props.build_profile_index = min(index, len(props.build_profiles) - 1) if index else 0
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_build_profile_move(bpy.types.Operator):
    """Move the selected build profile up or down"""

    bl_idname = "geotools.build_profile_move"
    bl_label = "Move Build Profile"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=(("UP", "Up", ""), ("DOWN", "Down", "")), default="UP")

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_profile_poll(cls, context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = context.scene.geotools
        index = props.build_profile_index
        target = index - 1 if self.direction == "UP" else index + 1
        if not 0 <= target < len(props.build_profiles):
            return {"CANCELLED"}
        props.build_profiles.move(index, target)
        props.build_profile_index = target
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_UL_build_profiles(bpy.types.UIList):
    """Build profiles: editable name and the rusty-tiles profile (or .glb)."""

    bl_idname = "GEOTOOLS_UL_build_profiles"

    def draw_item(
        self,
        context: bpy.types.Context,
        layout: bpy.types.UILayout,
        data: bpy.types.AnyType,
        item: bpy.types.PropertyGroup,
        icon: int,
        active_data: bpy.types.AnyType,
        active_property: str,
        index: int = 0,
        flt_flag: int = 0,
    ) -> None:
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon="PRESET")
        if item.output_type == "GLB":
            row.label(text=".glb")  # the quality is a rusty-tiles setting
        elif item.output_type == "VECTOR":
            row.label(text="Vectors")
        else:
            row.label(text=item.bl_rna.properties["profile"].enum_items[item.profile].name)


classes = (
    GEOTOOLS_OT_build_profile_add,
    GEOTOOLS_OT_build_profile_remove,
    GEOTOOLS_OT_build_profile_move,
    GEOTOOLS_UL_build_profiles,
)

register, unregister = bpy.utils.register_classes_factory(classes)
