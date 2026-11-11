"""Shader node walking shared by the scene summary and the publish export.

See ``summarise`` for how Blender 5.2 expresses alpha modes in the node tree.
"""

from __future__ import annotations

import bpy

_MAX_REROUTES = 64


def from_link(socket: bpy.types.NodeSocket) -> tuple[bpy.types.Node, bpy.types.NodeSocket] | None:
    """The node and output socket feeding ``socket``, skipping reroutes and muted links."""
    seen = 0
    while socket.is_linked and seen < _MAX_REROUTES:
        link = next((lk for lk in socket.links if not lk.is_muted), None)
        if link is None:
            return None
        node = link.from_node
        if node.bl_idname != "NodeReroute":
            return node, link.from_socket
        socket = node.inputs[0]
        seen += 1
    return None


def from_node(socket: bpy.types.NodeSocket) -> bpy.types.Node | None:
    """The node feeding ``socket``, skipping reroutes and muted links."""
    found = from_link(socket)
    return found[0] if found is not None else None


def is_math(node: bpy.types.Node | None, *operations: str) -> bool:
    return node is not None and node.bl_idname == "ShaderNodeMath" and node.operation in operations


def alpha_clip_nodes(principled: bpy.types.Node) -> list[bpy.types.Node]:
    """The Math nodes of an alpha-clip pattern feeding Alpha, or ``[]``."""
    first = from_node(principled.inputs["Alpha"])
    if is_math(first, "ROUND", "GREATER_THAN"):
        return [first]
    if (
        is_math(first, "SUBTRACT")
        and not first.inputs[0].is_linked
        and abs(first.inputs[0].default_value - 1.0) < 1e-6
    ):
        second = from_node(first.inputs[1])
        if is_math(second, "LESS_THAN"):
            return [first, second]
    return []


def alpha_cutoff(principled: bpy.types.Node) -> float:
    """The cutoff of the alpha-clip pattern feeding Alpha (0.5 for ``ROUND``)."""
    nodes = alpha_clip_nodes(principled)
    if not nodes or nodes[-1].operation == "ROUND":
        return 0.5
    threshold = nodes[-1].inputs[1]
    return 0.5 if threshold.is_linked else float(threshold.default_value)


def blend_mode(principled: bpy.types.Node | None) -> str:
    """``OPAQUE``, ``CLIP`` or ``BLEND``; see the ``summarise`` module docstring."""
    if principled is None:
        return "OPAQUE"
    alpha = principled.inputs["Alpha"]
    if not alpha.is_linked or from_node(alpha) is None:
        return "OPAQUE" if alpha.default_value >= 1.0 else "BLEND"
    return "CLIP" if alpha_clip_nodes(principled) else "BLEND"


def output_node(tree: bpy.types.NodeTree) -> bpy.types.Node | None:
    for target in ("ALL", "EEVEE", "CYCLES"):
        node = tree.get_output_node(target)
        if node is not None:
            return node
    return None


def principled_of(material: bpy.types.Material) -> bpy.types.Node | None:
    """The Principled BSDF feeding the material output's Surface, if any."""
    tree = material.node_tree
    if tree is None:
        return None
    output = output_node(tree)
    if output is None:
        return None
    surface = from_node(output.inputs["Surface"])
    if surface is not None and surface.bl_idname == "ShaderNodeBsdfPrincipled":
        return surface
    return None


def upstream(start: bpy.types.Node) -> list[bpy.types.Node]:
    """``start`` and every node linked into it, directly or not (unmuted links)."""
    found: dict[str, bpy.types.Node] = {start.name: start}
    stack = [start]
    while stack:
        node = stack.pop()
        for socket in node.inputs:
            for link in socket.links:
                if link.is_muted:
                    continue
                src = link.from_node
                if src.name not in found:
                    found[src.name] = src
                    stack.append(src)
    return list(found.values())
