"""
Fusion360 Command Handler

Executes commands using the Fusion 360 API.  Every method in this class
is called on the **main thread** (via EventBridge), so Fusion API access
is safe.
"""

import ast
import base64
import io
import math
import os
import tempfile
import time
import traceback
from contextlib import redirect_stdout

import adsk.cam
import adsk.core
import adsk.fusion

from . import get_logger
from . import hints as _hints

log = get_logger("handler")


class CommandHandler:
    """Runs Fusion API operations.  Instantiated once; reused across requests."""

    def __init__(self):
        self.app = adsk.core.Application.get()
        self.ui = self.app.userInterface

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    _COMMANDS = None  # populated lazily

    # Canonical camera presets, (eye_dir, up_vec) in Fusion's Z-up world.
    # Shared by render_view and export_view_sheet.
    _VIEW_DIRS = {
        "iso": ((1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
        "iso_ne": ((1.0, 1.0, 1.0), (0.0, 0.0, 1.0)),
        "iso_nw": ((-1.0, 1.0, 1.0), (0.0, 0.0, 1.0)),
        "iso_sw": ((-1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
        "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
        "back": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        "top": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
        "bottom": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
        "right": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        "left": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    }

    # Commands that can change body mass / bbox / body count.  Only these
    # get before/after snapshots so agents can sanity-check without a render.
    _MUTATION_COMMANDS = frozenset(
        {
            # feature ops
            "extrude",
            "revolve",
            "sweep",
            "loft",
            "fillet",
            "chamfer",
            "shell",
            "mirror",
            "create_hole",
            "rectangular_pattern",
            "circular_pattern",
            "create_thread",
            "draft_faces",
            "split_body",
            "split_face",
            "offset_faces",
            "scale_body",
            "suppress_feature",
            "unsuppress_feature",
            # body ops
            "move_body",
            "boolean_operation",
            # primitives
            "create_box",
            "create_cylinder",
            "create_sphere",
            "create_torus",
            # surface / sheet metal (can produce / thicken bodies)
            "thicken_surface",
            "patch_surface",
            "stitch_surfaces",
            "ruled_surface",
            "trim_surface",
            "create_flange",
            "create_bend",
            "flat_pattern",
            "unfold",
            # scene-wide
            "delete_all",
            "undo",
            # parametric & agent-authored changes
            "set_parameter",
            "execute_code",
            # CAM writes
            "cam_update_operation_parameters",
            "cam_update_setup_machine_params",
        }
    )

    def execute_command(self, command: dict) -> dict:
        """Route *command* to the correct handler; return a response dict."""
        if self._COMMANDS is None:
            self.__class__._COMMANDS = {
                # scene / query
                "get_scene_info": self.get_scene_info,
                "get_object_info": self.get_object_info,
                "list_components": self.list_components,
                # sketch
                "create_sketch": self.create_sketch,
                "draw_rectangle": self.draw_rectangle,
                "draw_circle": self.draw_circle,
                "draw_line": self.draw_line,
                "draw_arc": self.draw_arc,
                "draw_spline": self.draw_spline,
                "create_polygon": self.create_polygon,
                "add_constraint": self.add_constraint,
                "add_dimension": self.add_dimension,
                "offset_curve": self.offset_curve,
                "trim_curve": self.trim_curve,
                "extend_curve": self.extend_curve,
                "project_geometry": self.project_geometry,
                # features
                "extrude": self.extrude,
                "revolve": self.revolve,
                "sweep": self.sweep,
                "loft": self.loft,
                "fillet": self.fillet,
                "chamfer": self.chamfer,
                "shell": self.shell,
                "mirror": self.mirror,
                "create_hole": self.create_hole,
                "rectangular_pattern": self.rectangular_pattern,
                "circular_pattern": self.circular_pattern,
                "create_thread": self.create_thread,
                "draft_faces": self.draft_faces,
                "split_body": self.split_body,
                "split_face": self.split_face,
                "offset_faces": self.offset_faces,
                "scale_body": self.scale_body,
                "suppress_feature": self.suppress_feature,
                "unsuppress_feature": self.unsuppress_feature,
                # body operations
                "move_body": self.move_body,
                "rename_body": self.rename_body,
                "export_stl": self.export_stl,
                "export_step": self.export_step,
                "export_f3d": self.export_f3d,
                "export_view_sheet": self.export_view_sheet,
                "boolean_operation": self.boolean_operation,
                "delete_all": self.delete_all,
                "undo": self.undo,
                # direct primitives
                "create_box": self.create_box,
                "create_cylinder": self.create_cylinder,
                "create_sphere": self.create_sphere,
                "create_torus": self.create_torus,
                # construction geometry
                "create_construction_plane": self.create_construction_plane,
                "create_construction_axis": self.create_construction_axis,
                # assembly
                "create_component": self.create_component,
                "add_joint": self.add_joint,
                "create_as_built_joint": self.create_as_built_joint,
                "create_rigid_group": self.create_rigid_group,
                # inspection / analysis
                "measure_distance": self.measure_distance,
                "measure_angle": self.measure_angle,
                "get_physical_properties": self.get_physical_properties,
                "create_section_analysis": self.create_section_analysis,
                "check_interference": self.check_interference,
                # appearance
                "set_appearance": self.set_appearance,
                # parameters
                "get_parameters": self.get_parameters,
                "create_parameter": self.create_parameter,
                "set_parameter": self.set_parameter,
                "delete_parameter": self.delete_parameter,
                # surface operations
                "patch_surface": self.patch_surface,
                "stitch_surfaces": self.stitch_surfaces,
                "thicken_surface": self.thicken_surface,
                "ruled_surface": self.ruled_surface,
                "trim_surface": self.trim_surface,
                # sheet metal
                "create_flange": self.create_flange,
                "create_bend": self.create_bend,
                "flat_pattern": self.flat_pattern,
                "unfold": self.unfold,
                # code execution
                "execute_code": self.execute_code,
                # CAM — existing
                "cam_list_setups": self.cam_list_setups,
                "cam_list_operations": self.cam_list_operations,
                "cam_get_operation_info": self.cam_get_operation_info,
                "cam_create_setup": self.cam_create_setup,
                "cam_delete_setup": self.cam_delete_setup,
                "cam_create_operation": self.cam_create_operation,
                "cam_generate_toolpath": self.cam_generate_toolpath,
                "cam_post_process": self.cam_post_process,
                # CAM — extended (Step 2)
                "cam_get_toolpath_status": self.cam_get_toolpath_status,
                "cam_get_operation_details": self.cam_get_operation_details,
                "cam_update_operation_parameters": self.cam_update_operation_parameters,
                "cam_get_tools": self.cam_get_tools,
                "cam_get_machining_time": self.cam_get_machining_time,
                "cam_get_library_tools": self.cam_get_library_tools,
                "cam_update_setup_machine_params": self.cam_update_setup_machine_params,
                "cam_get_nc_programs": self.cam_get_nc_programs,
                "cam_set_operation_geometry": self.cam_set_operation_geometry,
                "cam_set_operation_tool": self.cam_set_operation_tool,
                "cam_create_document_tool": self.cam_create_document_tool,
                # health
                "ping": self.ping,
                # design type safety
                "get_design_type": self.get_design_type,
                "set_design_type": self.set_design_type,
                # perception
                "render_view": self.render_view,
            }

        cmd_type = command.get("type")
        params = command.get("params", {})

        handler = self._COMMANDS.get(cmd_type)
        if handler is None:
            # Infrastructure-level failure (not an application error) —
            # keep the legacy error envelope so the client raises.
            return {"status": "error", "message": f"Unknown command: {cmd_type}"}

        is_mutation = cmd_type in self._MUTATION_COMMANDS
        snap_before = self._snapshot() if is_mutation else None

        try:
            t0 = time.monotonic()
            result = handler(**params)
            elapsed = time.monotonic() - t0
            log.debug("%s completed in %.3fs", cmd_type, elapsed)
        except Exception as exc:
            log.error("%s raised: %s", cmd_type, exc)
            error_kind, hint_list = _hints.classify(exc)
            return {
                "status": "success",
                "result": {
                    "ok": False,
                    "error_kind": error_kind,
                    "error_message": str(exc) or exc.__class__.__name__,
                    "hints": hint_list,
                    "traceback": traceback.format_exc(),
                },
            }

        if not isinstance(result, dict):
            result = {"value": result}
        result.setdefault("ok", True)

        if is_mutation and snap_before is not None:
            snap_after = self._snapshot()
            if snap_after is not None:
                result["deltas"] = self._compute_deltas(snap_before, snap_after)

        return {"status": "success", "result": result}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _design(self):
        doc = self.app.activeDocument
        if doc is None:
            raise RuntimeError("No active document")
        d = doc.products.itemByProductType("DesignProductType")
        if d is None:
            raise RuntimeError("No active design")
        return d

    def _root(self):
        return self._design().rootComponent

    def _require_parametric(self):
        """Raise if the design is in direct modeling mode."""
        design = self._design()
        if design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
            raise RuntimeError(
                "This tool requires parametric mode. "
                "Call set_design_type('parametric') first, or use "
                "'Capture Design History' in the Fusion UI."
            )

    def _last_sketch(self):
        root = self._root()
        if root.sketches.count == 0:
            raise RuntimeError("No sketch available — create one first")
        return root.sketches.item(root.sketches.count - 1)

    def _sketch_by_name(self, name: str):
        root = self._root()
        for i in range(root.sketches.count):
            s = root.sketches.item(i)
            if s.name == name:
                return s
        raise RuntimeError(f"Sketch '{name}' not found")

    def _body_by_name(self, name: str):
        root = self._root()
        # Search root bodies first
        for i in range(root.bRepBodies.count):
            b = root.bRepBodies.item(i)
            if b.name == name:
                return b
        # Search bodies inside components via occurrence proxies (assembly design)
        # Returns proxy body in root coordinate space for correct boolean ops
        for occ in root.allOccurrences:
            for i in range(occ.bRepBodies.count):
                b = occ.bRepBodies.item(i)
                if b.name == name:
                    return b
        raise RuntimeError(f"Body '{name}' not found")

    def _component_by_name(self, name: str):
        root = self._root()
        if root.name == name:
            return root
        for occ in root.allOccurrences:
            if occ.component.name == name:
                return occ.component
        raise RuntimeError(f"Component '{name}' not found")

    def _construction_plane(self, plane: str):
        root = self._root()
        m = {
            "xy": root.xYConstructionPlane,
            "yz": root.yZConstructionPlane,
            "xz": root.xZConstructionPlane,
        }
        p = m.get(plane)
        if p is None:
            raise RuntimeError(f"Unknown plane '{plane}' — use xy, yz, or xz")
        return p

    def _construction_axis(self, axis: str):
        root = self._root()
        m = {
            "x": root.xConstructionAxis,
            "y": root.yConstructionAxis,
            "z": root.zConstructionAxis,
        }
        a = m.get(axis)
        if a is None:
            raise RuntimeError(f"Unknown axis '{axis}' — use x, y, or z")
        return a

    @staticmethod
    def _operation_type(name: str):
        m = {
            "new_body": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
            "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
            "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
            "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
        }
        t = m.get(name)
        if t is None:
            raise RuntimeError(
                f"Unknown operation '{name}' — use new_body/join/cut/intersect"
            )
        return t

    @staticmethod
    def _is_tangent_edge(edge) -> bool:
        """Return True if the two faces meeting at *edge* are tangent (smooth seam).

        Tangent seam edges — created by prior fillets/chamfers — cannot be
        filleted again and should be excluded from edge selections.
        """
        if edge.faces.count != 2:
            return False
        pt = edge.pointOnEdge
        ok1, n1 = edge.faces.item(0).evaluator.getNormalAtPoint(pt)
        ok2, n2 = edge.faces.item(1).evaluator.getNormalAtPoint(pt)
        if not (ok1 and ok2):
            return False
        dot = abs(n1.x * n2.x + n1.y * n2.y + n1.z * n2.z)
        return dot > 0.9994  # ~2° threshold

    def _select_edges(self, body, selection: str):
        """Return an ObjectCollection of edges based on *selection*."""
        coll = adsk.core.ObjectCollection.create()
        bbox = body.boundingBox
        edge_count = body.edges.count

        if selection == "all":
            for i in range(edge_count):
                edge = body.edges.item(i)
                if not self._is_tangent_edge(edge):
                    coll.add(edge)
        elif selection == "top":
            threshold = bbox.maxPoint.z - 0.001
            for i in range(edge_count):
                edge = body.edges.item(i)
                if edge.pointOnEdge.z > threshold and not self._is_tangent_edge(edge):
                    coll.add(edge)
        elif selection == "bottom":
            threshold = bbox.minPoint.z + 0.001
            for i in range(edge_count):
                edge = body.edges.item(i)
                if edge.pointOnEdge.z < threshold and not self._is_tangent_edge(edge):
                    coll.add(edge)
        elif selection == "vertical":
            for i in range(edge_count):
                edge = body.edges.item(i)
                sp = edge.startVertex.geometry
                ep = edge.endVertex.geometry
                if (
                    abs(sp.x - ep.x) < 0.001
                    and abs(sp.y - ep.y) < 0.001
                    and not self._is_tangent_edge(edge)
                ):
                    coll.add(edge)
        else:
            raise RuntimeError(
                f"Unknown edge_selection '{selection}' — use all/top/bottom/vertical"
            )

        if coll.count == 0:
            raise RuntimeError(f"No edges matched selection '{selection}'")
        return coll

    def _select_faces(self, body, selection: str):
        """Return an ObjectCollection of faces based on *selection*."""
        coll = adsk.core.ObjectCollection.create()
        bbox = body.boundingBox
        face_count = body.faces.count

        if selection == "all":
            for i in range(face_count):
                coll.add(body.faces.item(i))
        elif selection == "top":
            threshold = bbox.maxPoint.z - 0.001
            for i in range(face_count):
                face = body.faces.item(i)
                if face.boundingBox.maxPoint.z > threshold:
                    coll.add(face)
        elif selection == "bottom":
            threshold = bbox.minPoint.z + 0.001
            for i in range(face_count):
                face = body.faces.item(i)
                if face.boundingBox.minPoint.z < threshold:
                    coll.add(face)
        elif selection == "vertical":
            for i in range(face_count):
                face = body.faces.item(i)
                try:
                    _, normal_vec = face.evaluator.getNormalAtPoint(face.pointOnFace)
                    if abs(normal_vec.z) < 0.1:
                        coll.add(face)
                except Exception:
                    pass
        else:
            raise RuntimeError(
                f"Unknown face_selection '{selection}' — use all/top/bottom/vertical"
            )

        if coll.count == 0:
            raise RuntimeError(f"No faces matched selection '{selection}'")
        return coll

    # ------------------------------------------------------------------
    # Scene / Query
    # ------------------------------------------------------------------

    def get_scene_info(self):
        design = self._design()
        root = self._root()

        bodies = []
        for i in range(root.bRepBodies.count):
            b = root.bRepBodies.item(i)
            bodies.append(
                {
                    "name": b.name,
                    "volume": b.volume,
                    "area": b.area,
                    "material": b.material.name if b.material else None,
                    "is_visible": b.isVisible,
                }
            )

        sketches = []
        for i in range(root.sketches.count):
            s = root.sketches.item(i)
            sketches.append(
                {
                    "name": s.name,
                    "profile_count": s.profiles.count,
                    "is_visible": s.isVisible,
                }
            )

        return {
            "design_name": design.parentDocument.name,
            "design_type": design.productType,
            "bodies": bodies,
            "sketches": sketches,
            "bodies_count": root.bRepBodies.count,
            "sketches_count": root.sketches.count,
            "features_count": root.features.count,
            "timeline_count": (
                design.timeline.count if hasattr(design, "timeline") else 0
            ),
            "camera": self._camera_info(),
        }

    def get_object_info(self, name: str):
        root = self._root()

        # bodies
        for i in range(root.bRepBodies.count):
            b = root.bRepBodies.item(i)
            if b.name == name:
                return {
                    "found": True,
                    "type": "body",
                    "name": name,
                    "volume": b.volume,
                    "area": b.area,
                    "material": b.material.name if b.material else None,
                    "is_visible": b.isVisible,
                    "faces_count": b.faces.count,
                    "edges_count": b.edges.count,
                    "vertices_count": b.vertices.count,
                    "bounding_box": self._bbox_dict(b.boundingBox),
                }

        # sketches
        for i in range(root.sketches.count):
            s = root.sketches.item(i)
            if s.name == name:
                return {
                    "found": True,
                    "type": "sketch",
                    "name": name,
                    "is_visible": s.isVisible,
                    "profile_count": s.profiles.count,
                    "curve_count": s.sketchCurves.count,
                }

        return {"found": False, "name": name}

    def list_components(self):
        root = self._root()
        components = [{"name": root.name, "is_root": True}]
        for occ in root.allOccurrences:
            components.append(
                {
                    "name": occ.component.name,
                    "is_root": False,
                    "is_visible": occ.isVisible,
                }
            )
        return {"components": components, "count": len(components)}

    # ------------------------------------------------------------------
    # Sketch
    # ------------------------------------------------------------------

    def create_sketch(self, plane: str = "xy", z_offset: float = None):
        root = self._root()

        if z_offset is not None and z_offset != 0:
            # Create an offset construction plane
            planes = root.constructionPlanes
            plane_input = planes.createInput()
            offset_val = adsk.core.ValueInput.createByReal(z_offset)
            plane_input.setByOffset(self._construction_plane(plane), offset_val)
            cp = planes.add(plane_input)
            sketch = root.sketches.add(cp)
        else:
            sketch = root.sketches.add(self._construction_plane(plane))

        return {"sketch_name": sketch.name, "plane": plane, "z_offset": z_offset}

    def draw_rectangle(
        self,
        width: float,
        height: float,
        origin_x: float = 0,
        origin_y: float = 0,
        origin_z: float = 0,
    ):
        sketch = self._last_sketch()
        p1 = adsk.core.Point3D.create(origin_x, origin_y, origin_z)
        p2 = adsk.core.Point3D.create(origin_x + width, origin_y + height, origin_z)
        sketch.sketchCurves.sketchLines.addTwoPointRectangle(p1, p2)
        return {"sketch": sketch.name, "width": width, "height": height}

    def draw_circle(
        self,
        radius: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
    ):
        sketch = self._last_sketch()
        c = adsk.core.Point3D.create(center_x, center_y, center_z)
        sketch.sketchCurves.sketchCircles.addByCenterRadius(c, radius)
        return {
            "sketch": sketch.name,
            "radius": radius,
            "center": [center_x, center_y, center_z],
        }

    def draw_line(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        start_z: float = 0,
        end_z: float = 0,
    ):
        sketch = self._last_sketch()
        sp = adsk.core.Point3D.create(start_x, start_y, start_z)
        ep = adsk.core.Point3D.create(end_x, end_y, end_z)
        sketch.sketchCurves.sketchLines.addByTwoPoints(sp, ep)
        return {
            "sketch": sketch.name,
            "start": [start_x, start_y, start_z],
            "end": [end_x, end_y, end_z],
        }

    def draw_arc(
        self,
        center_x: float,
        center_y: float,
        start_x: float,
        start_y: float,
        sweep_angle: float,
        center_z: float = 0,
        start_z: float = 0,
    ):
        sketch = self._last_sketch()
        center = adsk.core.Point3D.create(center_x, center_y, center_z)
        start = adsk.core.Point3D.create(start_x, start_y, start_z)
        sweep_rad = math.radians(sweep_angle)
        sketch.sketchCurves.sketchArcs.addByCenterStartSweep(center, start, sweep_rad)
        return {"sketch": sketch.name, "sweep_angle": sweep_angle}

    def draw_spline(self, spline_type: str, points: list, degree: int = 3):
        sketch = self._last_sketch()
        pts = adsk.core.ObjectCollection.create()
        for p in points:
            z = p[2] if len(p) > 2 else 0
            pts.add(adsk.core.Point3D.create(p[0], p[1], z))

        if spline_type == "fit_points":
            sketch.sketchCurves.sketchFittedSplines.add(pts)
        else:  # control_points
            sketch.sketchCurves.sketchControlPointSplines.add(pts, degree)
        return {
            "sketch": sketch.name,
            "spline_type": spline_type,
            "points_count": len(points),
        }

    def create_polygon(
        self,
        sides: int,
        radius: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
    ):
        sketch = self._last_sketch()
        # Draw inscribed polygon
        for i in range(sides):
            angle1 = 2 * math.pi * i / sides
            angle2 = 2 * math.pi * (i + 1) / sides
            p1 = adsk.core.Point3D.create(
                center_x + radius * math.cos(angle1),
                center_y + radius * math.sin(angle1),
                center_z,
            )
            p2 = adsk.core.Point3D.create(
                center_x + radius * math.cos(angle2),
                center_y + radius * math.sin(angle2),
                center_z,
            )
            sketch.sketchCurves.sketchLines.addByTwoPoints(p1, p2)
        return {"sketch": sketch.name, "sides": sides, "radius": radius}

    def add_constraint(
        self,
        constraint_type: str,
        entity_one: int = None,
        entity_two: int = None,
        symmetry_line: int = None,
        sketch_name: str = None,
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        constraints = sketch.geometricConstraints
        curves = list(sketch.sketchCurves)

        e1 = curves[entity_one] if entity_one is not None else None
        e2 = curves[entity_two] if entity_two is not None else None

        constraint_map = {
            "coincident": lambda: constraints.addCoincident(e1, e2),
            "parallel": lambda: constraints.addParallel(e1, e2),
            "perpendicular": lambda: constraints.addPerpendicular(e1, e2),
            "tangent": lambda: constraints.addTangent(e1, e2),
            "equal": lambda: constraints.addEqual(e1, e2),
            "fix": lambda: constraints.addFix(e1),
            "horizontal": lambda: constraints.addHorizontal(e1),
            "vertical": lambda: constraints.addVertical(e1),
            "concentric": lambda: constraints.addConcentric(e1, e2),
            "collinear": lambda: constraints.addCollinear(e1, e2),
            "smooth": lambda: constraints.addSmooth(e1, e2),
            "midpoint": lambda: constraints.addMidPoint(
                sketch.sketchPoints.item(entity_one), e2
            ),
            "symmetry": lambda: constraints.addSymmetry(e1, e2, curves[symmetry_line]),
        }

        if constraint_type not in constraint_map:
            raise RuntimeError(f"Unknown constraint type: {constraint_type}")

        constraint_map[constraint_type]()
        return {"sketch": sketch.name, "constraint_type": constraint_type}

    def add_dimension(
        self,
        dimension_type: str,
        value: float,
        entity_one: int = None,
        entity_two: int = None,
        sketch_name: str = None,
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        dims = sketch.sketchDimensions
        curves = list(sketch.sketchCurves)

        e1 = curves[entity_one] if entity_one is not None else None
        e2 = curves[entity_two] if entity_two is not None else None
        text_pt = adsk.core.Point3D.create(0, 0, 0)

        if dimension_type == "distance":
            dim = dims.addDistanceDimension(
                e1.startSketchPoint,
                e2.startSketchPoint,
                adsk.fusion.DimensionOrientations.AlignedDimensionOrientation,
                text_pt,
            )
        elif dimension_type == "horizontal":
            dim = dims.addDistanceDimension(
                e1.startSketchPoint,
                e2.startSketchPoint,
                adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
                text_pt,
            )
        elif dimension_type == "vertical":
            dim = dims.addDistanceDimension(
                e1.startSketchPoint,
                e2.startSketchPoint,
                adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
                text_pt,
            )
        elif dimension_type == "angular":
            dim = dims.addAngularDimension(e1, e2, text_pt)
        elif dimension_type == "radial":
            dim = dims.addRadialDimension(e1, text_pt)
        elif dimension_type == "diameter":
            dim = dims.addDiameterDimension(e1, text_pt)
        else:
            raise RuntimeError(f"Unknown dimension type: {dimension_type}")

        dim.parameter.value = value
        return {"sketch": sketch.name, "dimension_type": dimension_type, "value": value}

    def offset_curve(
        self,
        curve_index: int,
        offset_distance: float,
        direction_x: float = 1,
        direction_y: float = 0,
        sketch_name: str = None,
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        curves = list(sketch.sketchCurves)
        curve = curves[curve_index]
        direction_pt = adsk.core.Point3D.create(direction_x, direction_y, 0)

        coll = adsk.core.ObjectCollection.create()
        coll.add(curve)
        sketch.offset(coll, direction_pt, offset_distance)
        return {"sketch": sketch.name, "offset_distance": offset_distance}

    def trim_curve(
        self, curve_index: int, point_x: float, point_y: float, sketch_name: str = None
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        curves = list(sketch.sketchCurves)
        curve = curves[curve_index]
        point = adsk.core.Point3D.create(point_x, point_y, 0)
        curve.trim(point)
        return {"sketch": sketch.name, "trimmed": True}

    def extend_curve(
        self, curve_index: int, point_x: float, point_y: float, sketch_name: str = None
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        curves = list(sketch.sketchCurves)
        curve = curves[curve_index]
        point = adsk.core.Point3D.create(point_x, point_y, 0)
        curve.extend(point)
        return {"sketch": sketch.name, "extended": True}

    def project_geometry(
        self, source_name: str, is_linked: bool = True, sketch_name: str = None
    ):
        sketch = (
            self._sketch_by_name(sketch_name) if sketch_name else self._last_sketch()
        )
        body = self._body_by_name(source_name)

        projected = []
        for edge in body.edges:
            proj = sketch.project(edge)
            projected.append(proj.count)

        return {
            "sketch": sketch.name,
            "source": source_name,
            "projected_curves": sum(projected),
        }

    # ------------------------------------------------------------------
    # Features
    # ------------------------------------------------------------------

    def extrude(
        self,
        height: float,
        profile_index: int = 0,
        operation: str = "new_body",
        direction: str = "positive",
    ):
        self._require_parametric()
        root = self._root()
        sketch = self._last_sketch()
        if sketch.profiles.count == 0:
            raise RuntimeError("No profiles in sketch")
        profile = sketch.profiles.item(profile_index)

        ext_feats = root.features.extrudeFeatures
        ext_input = ext_feats.createInput(profile, self._operation_type(operation))
        dist = adsk.core.ValueInput.createByReal(height)
        if direction == "symmetric":
            ext_input.setSymmetricExtent(dist, True)
        else:
            ext_input.setDistanceExtent(direction == "negative", dist)

        feat = ext_feats.add(ext_input)

        body_name = None
        if operation == "new_body" and feat.bodies.count > 0:
            body = feat.bodies.item(0)
            body.name = feat.name
            body_name = feat.name

        return {
            "feature_name": feat.name,
            "body_name": body_name,
            "height": height,
            "operation": operation,
            "direction": direction,
        }

    def revolve(
        self,
        angle: float,
        profile_index: int = 0,
        axis_origin_x: float = 0,
        axis_origin_y: float = 0,
        axis_origin_z: float = 0,
        axis_direction_x: float = 1,
        axis_direction_y: float = 0,
        axis_direction_z: float = 0,
        operation: str = "new_body",
    ):
        root = self._root()
        sketch = self._last_sketch()
        if sketch.profiles.count == 0:
            raise RuntimeError("No profiles in sketch")
        profile = sketch.profiles.item(profile_index)

        # Determine axis entity first (required for createInput)
        axis_entity = None
        is_x = abs(axis_direction_x) > 0.99 and abs(axis_direction_y) < 0.01
        is_y = abs(axis_direction_y) > 0.99 and abs(axis_direction_x) < 0.01
        is_z = abs(axis_direction_z) > 0.99 and abs(axis_direction_x) < 0.01
        if is_x and abs(axis_direction_z) < 0.01:
            axis_entity = root.xConstructionAxis
        elif is_y and abs(axis_direction_z) < 0.01:
            axis_entity = root.yConstructionAxis
        elif is_z and abs(axis_direction_y) < 0.01:
            axis_entity = root.zConstructionAxis
        else:
            # Create construction line in sketch
            origin = adsk.core.Point3D.create(
                axis_origin_x, axis_origin_y, axis_origin_z
            )
            end_pt = adsk.core.Point3D.create(
                axis_origin_x + axis_direction_x * 10,
                axis_origin_y + axis_direction_y * 10,
                axis_origin_z + axis_direction_z * 10,
            )
            line = sketch.sketchCurves.sketchLines.addByTwoPoints(origin, end_pt)
            line.isConstruction = True
            axis_entity = line

        rev_feats = root.features.revolveFeatures
        rev_input = rev_feats.createInput(
            profile, axis_entity, self._operation_type(operation)
        )

        angle_val = adsk.core.ValueInput.createByString(f"{angle} deg")
        rev_input.setAngleExtent(False, angle_val)

        feat = rev_feats.add(rev_input)
        return {"feature_name": feat.name, "angle": angle, "operation": operation}

    def sweep(
        self,
        profile_index: int,
        path_sketch_name: str,
        path_curve_index: int = 0,
        operation: str = "new_body",
    ):
        root = self._root()
        sketch = self._last_sketch()
        path_sketch = self._sketch_by_name(path_sketch_name)

        if sketch.profiles.count == 0:
            raise RuntimeError("No profiles in sketch")
        profile = sketch.profiles.item(profile_index)

        path_curves = list(path_sketch.sketchCurves)
        path_curve = path_curves[path_curve_index]

        path = root.features.createPath(path_curve)

        sweep_feats = root.features.sweepFeatures
        sweep_input = sweep_feats.createInput(
            profile, path, self._operation_type(operation)
        )
        feat = sweep_feats.add(sweep_input)
        return {"feature_name": feat.name, "operation": operation}

    def loft(self, profile_sketch_names: list, operation: str = "new_body"):
        root = self._root()
        loft_feats = root.features.loftFeatures
        loft_input = loft_feats.createInput(self._operation_type(operation))

        for sketch_name in profile_sketch_names:
            sketch = self._sketch_by_name(sketch_name)
            if sketch.profiles.count == 0:
                raise RuntimeError(f"No profiles in sketch '{sketch_name}'")
            loft_input.loftSections.add(sketch.profiles.item(0))

        feat = loft_feats.add(loft_input)
        return {
            "feature_name": feat.name,
            "operation": operation,
            "profile_count": len(profile_sketch_names),
        }

    def fillet(
        self,
        radius: float,
        body_name: str = None,
        body_index: int = 0,
        edge_selection: str = "all",
    ):
        self._require_parametric()
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )
        edges = self._select_edges(body, edge_selection)

        fillets = root.features.filletFeatures
        inp = fillets.createInput()
        inp.addConstantRadiusEdgeSet(
            edges, adsk.core.ValueInput.createByReal(radius), True
        )
        feat = fillets.add(inp)
        return {"feature_name": feat.name, "radius": radius, "edges_count": edges.count}

    def chamfer(
        self,
        distance: float,
        body_name: str = None,
        body_index: int = 0,
        edge_selection: str = "all",
    ):
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )
        edges = self._select_edges(body, edge_selection)

        chamfers = root.features.chamferFeatures
        inp = chamfers.createInput(edges, True)
        inp.setToEqualDistance(adsk.core.ValueInput.createByReal(distance))
        feat = chamfers.add(inp)
        return {
            "feature_name": feat.name,
            "distance": distance,
            "edges_count": edges.count,
        }

    def shell(
        self,
        thickness: float,
        body_name: str = None,
        body_index: int = 0,
        face_selection: str = "top",
    ):
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )

        faces = adsk.core.ObjectCollection.create()
        bbox = body.boundingBox

        if face_selection == "top":
            threshold = bbox.maxPoint.z - 0.001
            for face in body.faces:
                if face.boundingBox.maxPoint.z > threshold:
                    faces.add(face)
        elif face_selection == "bottom":
            threshold = bbox.minPoint.z + 0.001
            for face in body.faces:
                if face.boundingBox.minPoint.z < threshold:
                    faces.add(face)
        else:
            raise RuntimeError(
                f"Unknown face_selection '{face_selection}' — use top/bottom"
            )

        if faces.count == 0:
            raise RuntimeError(f"No faces matched '{face_selection}'")

        shells = root.features.shellFeatures
        body_coll = adsk.core.ObjectCollection.create()
        body_coll.add(body)
        inp = shells.createInput(body_coll)
        inp.facesToRemove = faces
        inp.insideThickness = adsk.core.ValueInput.createByReal(thickness)
        feat = shells.add(inp)
        return {
            "feature_name": feat.name,
            "thickness": thickness,
            "faces_removed": faces.count,
        }

    def mirror(self, mirror_plane: str, body_name: str = None, body_index: int = 0):
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )

        entities = adsk.core.ObjectCollection.create()
        entities.add(body)

        mirrors = root.features.mirrorFeatures
        inp = mirrors.createInput(entities, self._construction_plane(mirror_plane))
        feat = mirrors.add(inp)
        return {"feature_name": feat.name, "mirror_plane": mirror_plane}

    def create_hole(
        self,
        diameter: float,
        depth: float,
        body_name: str = None,
        body_index: int = 0,
        face_selection: str = "top",
        center_x: float = 0,
        center_y: float = 0,
    ):
        root = self._root()
        body = (
            self._body_by_name(body_name)
            if body_name
            else root.bRepBodies.item(body_index)
        )

        # Find the target face
        bbox = body.boundingBox
        target_face = None
        if face_selection == "top":
            threshold = bbox.maxPoint.z - 0.001
            for face in body.faces:
                if face.boundingBox.maxPoint.z > threshold:
                    target_face = face
                    break
        elif face_selection == "bottom":
            threshold = bbox.minPoint.z + 0.001
            for face in body.faces:
                if face.boundingBox.minPoint.z < threshold:
                    target_face = face
                    break

        if target_face is None:
            raise RuntimeError(f"No face found for selection '{face_selection}'")

        # Create a sketch point for the hole center
        sketch = root.sketches.add(target_face)
        center = adsk.core.Point3D.create(center_x, center_y, 0)
        sketch_pt = sketch.sketchPoints.add(center)

        # Create hole feature
        holes = root.features.holeFeatures
        hole_input = holes.createSimpleInput(
            adsk.core.ValueInput.createByReal(diameter / 2)
        )
        hole_input.setPositionBySketchPoint(sketch_pt)
        hole_input.setDistanceExtent(adsk.core.ValueInput.createByReal(depth))

        feat = holes.add(hole_input)
        return {"feature_name": feat.name, "diameter": diameter, "depth": depth}

    def rectangular_pattern(
        self,
        body_name: str,
        x_count: int = 1,
        x_spacing: float = 1.0,
        y_count: int = 1,
        y_spacing: float = 1.0,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        bodies = adsk.core.ObjectCollection.create()
        bodies.add(body)

        patterns = root.features.rectangularPatternFeatures
        inp = patterns.createInput(
            bodies,
            root.xConstructionAxis,
            adsk.core.ValueInput.createByReal(x_count),
            adsk.core.ValueInput.createByReal(x_spacing),
            adsk.fusion.PatternDistanceType.SpacingPatternDistanceType,
        )
        inp.setDirectionTwo(
            root.yConstructionAxis,
            adsk.core.ValueInput.createByReal(y_count),
            adsk.core.ValueInput.createByReal(y_spacing),
        )
        feat = patterns.add(inp)
        return {"feature_name": feat.name, "x_count": x_count, "y_count": y_count}

    def circular_pattern(
        self, body_name: str, count: int, axis: str = "z", total_angle: float = 360
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        bodies = adsk.core.ObjectCollection.create()
        bodies.add(body)

        patterns = root.features.circularPatternFeatures
        inp = patterns.createInput(bodies, self._construction_axis(axis))
        inp.quantity = adsk.core.ValueInput.createByReal(count)
        inp.totalAngle = adsk.core.ValueInput.createByString(f"{total_angle} deg")
        feat = patterns.add(inp)
        return {"feature_name": feat.name, "count": count, "total_angle": total_angle}

    def create_thread(
        self,
        body_name: str,
        face_index: int,
        is_internal: bool = False,
        thread_type: str = "ISO Metric profile",
        thread_designation: str = "M10x1.5",
        thread_class: str = "6g",
        is_modeled: bool = False,
        is_full_length: bool = True,
        thread_length: float = None,
    ):
        root = self._root()
        body = self._body_by_name(body_name)
        face = body.faces.item(face_index)

        threads = root.features.threadFeatures
        thread_data = threads.threadDataQuery
        thread_data.threadType = thread_type

        inp = threads.createInput(face, thread_data)
        inp.isModeled = is_modeled
        inp.isFullLength = is_full_length
        if not is_full_length and thread_length:
            inp.threadLength = adsk.core.ValueInput.createByReal(thread_length)

        feat = threads.add(inp)
        return {"feature_name": feat.name, "thread_type": thread_type}

    def draft_faces(
        self,
        body_name: str,
        angle: float,
        face_selection: str = "vertical",
        pull_direction_plane: str = "xy",
        is_tangent_chain: bool = True,
    ):
        root = self._root()
        body = self._body_by_name(body_name)
        faces = self._select_faces(body, face_selection)

        drafts = root.features.draftFeatures
        inp = drafts.createInput(
            faces,
            self._construction_plane(pull_direction_plane),
            adsk.core.ValueInput.createByString(f"{angle} deg"),
            is_tangent_chain,
        )
        feat = drafts.add(inp)
        return {"feature_name": feat.name, "angle": angle}

    def split_body(
        self,
        body_name: str,
        splitting_plane: str = "xy",
        splitting_body: str = None,
        extend_tool: bool = True,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        splits = root.features.splitBodyFeatures
        if splitting_body:
            tool = self._body_by_name(splitting_body)
            inp = splits.createInput(body, tool, extend_tool)
        else:
            inp = splits.createInput(
                body, self._construction_plane(splitting_plane), extend_tool
            )
        feat = splits.add(inp)
        return {"feature_name": feat.name, "splitting_plane": splitting_plane}

    def split_face(
        self,
        body_name: str,
        face_indices: list = None,
        splitting_plane: str = "xy",
        extend_tool: bool = True,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        faces = adsk.core.ObjectCollection.create()
        if face_indices:
            for idx in face_indices:
                faces.add(body.faces.item(idx))
        else:
            for face in body.faces:
                faces.add(face)

        splits = root.features.splitFaceFeatures
        inp = splits.createInput(
            faces, self._construction_plane(splitting_plane), extend_tool
        )
        feat = splits.add(inp)
        return {"feature_name": feat.name}

    def offset_faces(
        self,
        body_name: str,
        distance: float,
        face_selection: str = "top",
        face_indices: list = None,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        if face_indices:
            faces = adsk.core.ObjectCollection.create()
            for idx in face_indices:
                faces.add(body.faces.item(idx))
        else:
            faces = self._select_faces(body, face_selection)

        offsets = root.features.offsetFeatures
        inp = offsets.createInput(
            faces,
            adsk.core.ValueInput.createByReal(distance),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        feat = offsets.add(inp)
        return {"feature_name": feat.name, "distance": distance}

    def scale_body(
        self,
        body_name: str,
        scale: float,
        scale_x: float = None,
        scale_y: float = None,
        scale_z: float = None,
        anchor_x: float = 0,
        anchor_y: float = 0,
        anchor_z: float = 0,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        bodies = adsk.core.ObjectCollection.create()
        bodies.add(body)

        anchor = adsk.core.Point3D.create(anchor_x, anchor_y, anchor_z)

        scales = root.features.scaleFeatures
        if scale_x is not None and scale_y is not None and scale_z is not None:
            inp = scales.createInput(
                bodies,
                anchor,
                adsk.core.ValueInput.createByReal(scale_x),
                adsk.core.ValueInput.createByReal(scale_y),
                adsk.core.ValueInput.createByReal(scale_z),
            )
        else:
            inp = scales.createInput(
                bodies, anchor, adsk.core.ValueInput.createByReal(scale)
            )
        feat = scales.add(inp)
        return {"feature_name": feat.name, "scale": scale}

    def suppress_feature(self, feature_name: str):
        design = self._design()
        for i in range(design.timeline.count):
            item = design.timeline.item(i)
            has_entity = hasattr(item, "entity") and item.entity
            if has_entity and item.entity.name == feature_name:
                item.isSuppressed = True
                return {"suppressed": True, "feature": feature_name}
        raise RuntimeError(f"Feature '{feature_name}' not found in timeline")

    def unsuppress_feature(self, feature_name: str):
        design = self._design()
        for i in range(design.timeline.count):
            item = design.timeline.item(i)
            has_entity = hasattr(item, "entity") and item.entity
            if has_entity and item.entity.name == feature_name:
                item.isSuppressed = False
                return {"unsuppressed": True, "feature": feature_name}
        raise RuntimeError(f"Feature '{feature_name}' not found in timeline")

    # ------------------------------------------------------------------
    # Body Operations
    # ------------------------------------------------------------------

    def rename_body(self, body_name: str, new_name: str):
        body = self._body_by_name(body_name)
        old_name = body.name
        body.name = new_name
        return {"renamed": True, "old_name": old_name, "new_name": new_name}

    def move_body(self, body_name: str, x: float = 0, y: float = 0, z: float = 0):
        root = self._root()
        body = self._body_by_name(body_name)

        move_feats = root.features.moveFeatures
        bodies = adsk.core.ObjectCollection.create()
        bodies.add(body)

        transform = adsk.core.Matrix3D.create()
        transform.translation = adsk.core.Vector3D.create(x, y, z)

        inp = move_feats.createInput(bodies, transform)
        feat = move_feats.add(inp)
        return {"feature_name": feat.name, "body": body_name, "translation": [x, y, z]}

    def export_stl(self, body_name: str, file_path: str = None):
        body = self._body_by_name(body_name)

        if file_path is None:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            file_path = os.path.join(desktop, f"{body_name}.stl")

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        export_mgr = self._design().exportManager
        occ = body.assemblyContext  # None if body is at root

        if occ is None:
            stl_opts = export_mgr.createSTLExportOptions(body, file_path)
            stl_opts.meshRefinement = (
                adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
            )
            export_mgr.execute(stl_opts)
            return {"exported": True, "body": body_name, "file_path": file_path}

        # Body lives in a component occurrence: hide siblings so the
        # occurrence export only contains the target body. Identify
        # siblings by entityToken, not name, to handle same-name bodies.
        target_token = body.entityToken
        hidden = []
        for i in range(occ.bRepBodies.count):
            sibling = occ.bRepBodies.item(i)
            if sibling.entityToken != target_token and sibling.isVisible:
                sibling.isVisible = False
                hidden.append(sibling)

        try:
            stl_opts = export_mgr.createSTLExportOptions(occ, file_path)
            stl_opts.meshRefinement = (
                adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
            )
            export_mgr.execute(stl_opts)
        finally:
            for sibling in hidden:
                sibling.isVisible = True

        return {"exported": True, "body": body_name, "file_path": file_path}

    def export_step(self, body_name: str, file_path: str = None):
        body = self._body_by_name(body_name)

        if file_path is None:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            file_path = os.path.join(desktop, f"{body_name}.step")

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        export_mgr = self._design().exportManager
        occ = body.assemblyContext  # None if body is at root

        if occ is None:
            step_opts = export_mgr.createSTEPExportOptions(file_path, body)
            export_mgr.execute(step_opts)
            return {"exported": True, "body": body_name, "file_path": file_path}

        # Body lives in a component occurrence: hide siblings so the
        # occurrence export only contains the target body. Identify
        # siblings by entityToken, not name, to handle same-name bodies.
        target_token = body.entityToken
        hidden = []
        for i in range(occ.bRepBodies.count):
            sibling = occ.bRepBodies.item(i)
            if sibling.entityToken != target_token and sibling.isVisible:
                sibling.isVisible = False
                hidden.append(sibling)

        try:
            step_opts = export_mgr.createSTEPExportOptions(file_path, occ)
            export_mgr.execute(step_opts)
        finally:
            for sibling in hidden:
                sibling.isVisible = True

        return {"exported": True, "body": body_name, "file_path": file_path}

    def export_f3d(self, file_path: str = None):
        design = self._design()
        doc_name = design.parentDocument.name

        if file_path is None:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            file_path = os.path.join(desktop, f"{doc_name}.f3d")

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        export_mgr = design.exportManager
        f3d_opts = export_mgr.createFusionArchiveExportOptions(file_path)
        export_mgr.execute(f3d_opts)

        return {"exported": True, "file_path": file_path}

    # ── view sheet ─────────────────────────────────────────────────────
    # Render canonical views (iso/front/top/right/...) as PNGs and emit
    # a self-contained HTML page suitable for print-to-PDF. Intended
    # audience: mechanical engineers who want a quick sense of the part.

    def _scene_center_and_radius(self):
        """Return (center, radius) covering all visible root bodies."""
        root = self._root()
        minp = [float("inf")] * 3
        maxp = [float("-inf")] * 3
        found = False

        def _grow(bb):
            nonlocal found
            for j, coord in enumerate(("x", "y", "z")):
                minp[j] = min(minp[j], getattr(bb.minPoint, coord))
                maxp[j] = max(maxp[j], getattr(bb.maxPoint, coord))
            found = True

        for i in range(root.bRepBodies.count):
            b = root.bRepBodies.item(i)
            if b.isVisible:
                _grow(b.boundingBox)
        for i in range(root.occurrences.count):
            occ = root.occurrences.item(i)
            if not occ.isVisible:
                continue
            for j in range(occ.bRepBodies.count):
                b = occ.bRepBodies.item(j)
                if b.isVisible:
                    _grow(b.boundingBox)

        if not found:
            return (0.0, 0.0, 0.0), 10.0
        center = tuple((minp[i] + maxp[i]) / 2 for i in range(3))
        span = max(maxp[i] - minp[i] for i in range(3))
        return center, max(span, 1.0)

    def _apply_view(self, view_name: str, center, radius: float):
        """Point the active viewport camera at *center* from *view_name*."""
        dir_vec, up_vec = self._VIEW_DIRS[view_name]
        length = math.sqrt(sum(c * c for c in dir_vec))
        dist = radius * 3.0  # give fit() headroom
        eye = tuple(center[i] + dir_vec[i] / length * dist for i in range(3))
        vp = self.app.activeViewport
        cam = vp.camera
        cam.isSmoothTransition = False
        cam.cameraType = adsk.core.CameraTypes.OrthographicCameraType
        cam.eye = adsk.core.Point3D.create(*eye)
        cam.target = adsk.core.Point3D.create(*center)
        cam.upVector = adsk.core.Vector3D.create(*up_vec)
        cam.isFitView = True
        vp.camera = cam
        vp.refresh()
        adsk.doEvents()

    def export_view_sheet(
        self,
        title: str = None,
        notes: str = "",
        views: list = None,
        image_size: list = None,
        output_dir: str = None,
    ):
        """Render canonical views as PNGs + a shareable HTML sheet.

        Args:
            title: heading on the sheet (default: document name).
            notes: free-form text rendered below the views (newlines
                preserved; HTML is escaped).
            views: ordered list of view names. Valid: iso, iso_ne,
                iso_nw, iso_sw, front, back, top, bottom, right, left.
                Default: ["iso", "front", "top", "right"].
            image_size: [width, height] in pixels (default [1200, 900]).
            output_dir: destination folder
                (default: ~/Desktop/<doc>_views_<timestamp>).
        """
        import base64
        import html
        import json as _json

        design = self._design()
        doc_name = design.parentDocument.name
        sheet_title = title or doc_name
        views = views or ["iso", "front", "top", "right"]
        image_size = image_size or [1200, 900]
        width, height = int(image_size[0]), int(image_size[1])

        unknown = [v for v in views if v not in self._VIEW_DIRS]
        if unknown:
            raise RuntimeError(
                f"Unknown views: {unknown}. Valid: {sorted(self._VIEW_DIRS)}"
            )

        if output_dir is None:
            ts = time.strftime("%Y%m%d_%H%M%S")
            output_dir = os.path.join(
                os.path.expanduser("~"),
                "Desktop",
                f"{doc_name}_views_{ts}",
            )
        os.makedirs(output_dir, exist_ok=True)

        vp = self.app.activeViewport
        orig = vp.camera
        orig_state = {
            "eye": (orig.eye.x, orig.eye.y, orig.eye.z),
            "target": (orig.target.x, orig.target.y, orig.target.z),
            "up": (orig.upVector.x, orig.upVector.y, orig.upVector.z),
            "type": orig.cameraType,
        }

        center, radius = self._scene_center_and_radius()

        rendered = []
        try:
            for view_name in views:
                self._apply_view(view_name, center, radius)
                png_path = os.path.join(output_dir, f"{view_name}.png")
                vp.saveAsImageFile(png_path, width, height)
                rendered.append({"view": view_name, "path": png_path})
        finally:
            cam = vp.camera
            cam.isSmoothTransition = False
            cam.cameraType = orig_state["type"]
            cam.eye = adsk.core.Point3D.create(*orig_state["eye"])
            cam.target = adsk.core.Point3D.create(*orig_state["target"])
            cam.upVector = adsk.core.Vector3D.create(*orig_state["up"])
            vp.camera = cam
            vp.refresh()

        # Build self-contained HTML with base64-embedded PNGs.
        figures = []
        for r in rendered:
            with open(r["path"], "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            label = r["view"].replace("_", " ").upper()
            figures.append(
                "<figure>"
                f'<img src="data:image/png;base64,{b64}" alt="{label}">'
                f"<figcaption>{label}</figcaption>"
                "</figure>"
            )

        notes_block = ""
        if notes:
            notes_block = (
                '<section class="notes"><h2>Notes</h2>'
                f"<pre>{html.escape(notes)}</pre></section>"
            )

        timestamp = time.strftime("%Y-%m-%d %H:%M")
        html_doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{html.escape(sheet_title)}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
           sans-serif; color: #111; max-width: 1400px;
           margin: 2rem auto; padding: 0 2rem; }}
  header {{ border-bottom: 1px solid #d0d0d0; padding-bottom: .75rem;
            margin-bottom: 2rem; }}
  header h1 {{ margin: 0; font-weight: 500; font-size: 1.6rem; }}
  header .meta {{ color: #666; font-size: .85rem; margin-top: .25rem; }}
  .views {{ display: grid; grid-template-columns: 1fr 1fr;
            gap: 1.25rem; }}
  figure {{ margin: 0; border: 1px solid #e0e0e0; padding: .5rem;
            background: #fafafa; }}
  figure img {{ width: 100%; display: block; background: #fff; }}
  figcaption {{ text-align: center; font-size: .75rem; color: #555;
                margin-top: .35rem; letter-spacing: .15em; }}
  .notes {{ margin-top: 2rem; padding-top: 1rem;
            border-top: 1px solid #e0e0e0; }}
  .notes h2 {{ font-size: 1rem; font-weight: 500; margin: 0 0 .5rem; }}
  .notes pre {{ font-family: inherit; white-space: pre-wrap;
                margin: 0; color: #333; }}
  @media print {{
    body {{ max-width: none; margin: 0; padding: 1cm; }}
    .views {{ gap: .5cm; }}
    figure {{ break-inside: avoid; }}
  }}
</style>
</head><body>
<header>
  <h1>{html.escape(sheet_title)}</h1>
  <div class="meta">{html.escape(doc_name)} * {timestamp}</div>
</header>
<section class="views">{"".join(figures)}</section>
{notes_block}
</body></html>
"""
        html_path = os.path.join(output_dir, "view_sheet.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_doc)

        # Sidecar manifest - machine-readable record of what was emitted.
        manifest = {
            "title": sheet_title,
            "document": doc_name,
            "views": rendered,
            "html_path": html_path,
            "image_size": [width, height],
        }
        with open(os.path.join(output_dir, "manifest.json"), "w") as f:
            _json.dump(manifest, f, indent=2)

        return {
            "html_path": html_path,
            "output_dir": output_dir,
            "views": rendered,
            "title": sheet_title,
            "image_size": [width, height],
        }

    def boolean_operation(
        self, target_body: str, tool_body: str, operation: str = "join"
    ):
        root = self._root()
        target = self._body_by_name(target_body)
        tool = self._body_by_name(tool_body)

        combine_feats = root.features.combineFeatures
        tool_coll = adsk.core.ObjectCollection.create()
        tool_coll.add(tool)

        op_map = {
            "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
            "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
            "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
        }
        op = op_map.get(operation)
        if op is None:
            raise RuntimeError(
                f"Unknown boolean op '{operation}' — use join/cut/intersect"
            )

        inp = combine_feats.createInput(target, tool_coll)
        inp.operation = op
        feat = combine_feats.add(inp)
        return {
            "feature_name": feat.name,
            "operation": operation,
            "target": target_body,
            "tool": tool_body,
        }

    def delete_all(self):
        design = self._design()
        root = self._root()
        deleted = 0
        failed = 0

        is_parametric = (
            design.designType == adsk.fusion.DesignTypes.ParametricDesignType
        )

        if is_parametric and hasattr(design, "timeline") and design.timeline is not None:
            tl = design.timeline
            # Snapshot count — iterate in reverse so earlier indices stay valid
            # as entities are removed.
            count = tl.count
            for i in range(count - 1, -1, -1):
                try:
                    entity = tl.item(i).entity
                    if entity is not None:
                        entity.deleteMe()
                        deleted += 1
                        adsk.doEvents()
                except Exception:
                    failed += 1

        # Direct-mode designs have no timeline; delete bodies directly.
        # Also used as a fallback when timeline deletion left bodies behind.
        remaining_bodies = root.bRepBodies.count
        if remaining_bodies > 0:
            for i in range(remaining_bodies - 1, -1, -1):
                try:
                    root.bRepBodies.item(i).deleteMe()
                    deleted += 1
                    adsk.doEvents()
                except Exception:
                    failed += 1

        # Delete any remaining sketches directly.
        remaining_sketches = root.sketches.count
        if remaining_sketches > 0:
            for i in range(remaining_sketches - 1, -1, -1):
                try:
                    root.sketches.item(i).deleteMe()
                    deleted += 1
                    adsk.doEvents()
                except Exception:
                    failed += 1

        bodies_after = root.bRepBodies.count
        return {
            "deleted": bodies_after == 0,
            "bodies_remaining": bodies_after,
            "items_deleted": deleted,
            "items_failed": failed,
        }

    def undo(self):
        design = self._design()
        type_before = design.designType

        cmd_def = self.ui.commandDefinitions.itemById("UndoCommand")
        if cmd_def:
            cmd_def.execute()

        # Check if undo silently switched design type (Parametric → Direct)
        adsk.doEvents()  # let Fusion process the undo
        type_after = design.designType
        if type_before != type_after:
            # Undo the undo — redo to restore original state
            redo_def = self.ui.commandDefinitions.itemById("RedoCommand")
            if redo_def:
                redo_def.execute()
                adsk.doEvents()
            raise RuntimeError(
                f"Undo aborted: would have changed design type from "
                f"{'Parametric' if type_before == 1 else 'Direct'} to "
                f"{'Parametric' if type_after == 1 else 'Direct'}. "
                f"The undo was automatically reversed (redo). "
                f"Delete the failed feature explicitly instead."
            )

        return {"undone": True, "design_type": type_after}

    # ------------------------------------------------------------------
    # Direct Primitives (via TemporaryBRepManager)
    # ------------------------------------------------------------------

    def create_box(
        self,
        length: float,
        width: float,
        height: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
    ):
        root = self._root()
        temp_brep = adsk.fusion.TemporaryBRepManager.get()

        # Box orientation matrix
        orient = adsk.core.OrientedBoundingBox3D.create(
            adsk.core.Point3D.create(center_x, center_y, center_z + height / 2),
            adsk.core.Vector3D.create(1, 0, 0),
            adsk.core.Vector3D.create(0, 1, 0),
            length,
            width,
            height,
        )

        box_body = temp_brep.createBox(orient)
        base_feat = root.features.baseFeatures.add()
        base_feat.startEdit()
        root.bRepBodies.add(box_body, base_feat)
        base_feat.finishEdit()

        return {"created": True, "length": length, "width": width, "height": height}

    def create_cylinder(
        self,
        radius: float,
        height: float,
        base_x: float = 0,
        base_y: float = 0,
        base_z: float = 0,
        axis: str = "z",
    ):
        root = self._root()
        temp_brep = adsk.fusion.TemporaryBRepManager.get()

        base_pt = adsk.core.Point3D.create(base_x, base_y, base_z)
        axis_vec = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
        top_pt = adsk.core.Point3D.create(
            base_x + axis_vec[0] * height,
            base_y + axis_vec[1] * height,
            base_z + axis_vec[2] * height,
        )

        cyl_body = temp_brep.createCylinderOrCone(base_pt, radius, top_pt, radius)

        base_feat = root.features.baseFeatures.add()
        base_feat.startEdit()
        root.bRepBodies.add(cyl_body, base_feat)
        base_feat.finishEdit()

        return {"created": True, "radius": radius, "height": height}

    def create_sphere(
        self,
        radius: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
    ):
        root = self._root()
        temp_brep = adsk.fusion.TemporaryBRepManager.get()

        center = adsk.core.Point3D.create(center_x, center_y, center_z)
        sphere_body = temp_brep.createSphere(center, radius)

        base_feat = root.features.baseFeatures.add()
        base_feat.startEdit()
        root.bRepBodies.add(sphere_body, base_feat)
        base_feat.finishEdit()

        return {"created": True, "radius": radius}

    def create_torus(
        self,
        major_radius: float,
        minor_radius: float,
        center_x: float = 0,
        center_y: float = 0,
        center_z: float = 0,
        axis: str = "z",
    ):
        root = self._root()
        temp_brep = adsk.fusion.TemporaryBRepManager.get()

        center = adsk.core.Point3D.create(center_x, center_y, center_z)
        axis_vec = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
        axis_vector = adsk.core.Vector3D.create(*axis_vec)

        torus_body = temp_brep.createTorus(
            center, axis_vector, major_radius, minor_radius
        )

        base_feat = root.features.baseFeatures.add()
        base_feat.startEdit()
        root.bRepBodies.add(torus_body, base_feat)
        base_feat.finishEdit()

        return {
            "created": True,
            "major_radius": major_radius,
            "minor_radius": minor_radius,
        }

    # ------------------------------------------------------------------
    # Construction Geometry
    # ------------------------------------------------------------------

    def create_construction_plane(
        self,
        method: str,
        plane: str = None,
        offset: float = None,
        angle: float = None,
        edge_name: str = None,
        plane_one: str = None,
        plane_two: str = None,
        point_one: list = None,
        point_two: list = None,
        point_three: list = None,
    ):
        root = self._root()
        planes = root.constructionPlanes
        inp = planes.createInput()

        if method == "offset":
            inp.setByOffset(
                self._construction_plane(plane),
                adsk.core.ValueInput.createByReal(offset),
            )
        elif method == "angle":
            inp.setByAngle(
                self._construction_axis(edge_name or "x"),
                adsk.core.ValueInput.createByString(f"{angle} deg"),
                self._construction_plane(plane),
            )
        elif method == "midplane":
            inp.setByTwoPlanes(
                self._construction_plane(plane_one), self._construction_plane(plane_two)
            )
        elif method == "three_points":
            p1 = adsk.core.Point3D.create(*point_one)
            p2 = adsk.core.Point3D.create(*point_two)
            p3 = adsk.core.Point3D.create(*point_three)
            inp.setByThreePoints(p1, p2, p3)
        elif method == "tangent":
            raise RuntimeError("Tangent plane needs face selection—use execute_code")
        else:
            raise RuntimeError(f"Unknown method: {method}")

        plane_obj = planes.add(inp)
        return {"created": True, "name": plane_obj.name, "method": method}

    def create_construction_axis(
        self,
        method: str,
        point_one: list = None,
        point_two: list = None,
        plane_one: str = None,
        plane_two: str = None,
        body_name: str = None,
        edge_index: int = None,
    ):
        root = self._root()
        axes = root.constructionAxes
        inp = axes.createInput()

        if method == "two_points":
            p1 = adsk.core.Point3D.create(*point_one)
            p2 = adsk.core.Point3D.create(*point_two)
            inp.setByTwoPoints(p1, p2)
        elif method == "intersection":
            inp.setByTwoPlanes(
                self._construction_plane(plane_one), self._construction_plane(plane_two)
            )
        elif method == "edge":
            body = self._body_by_name(body_name)
            edge = body.edges.item(edge_index)
            inp.setByEdge(edge)
        elif method == "perpendicular_at_point":
            p1 = adsk.core.Point3D.create(*point_one)
            inp.setByPerpendicularAtPoint(self._construction_plane(plane_one), p1)
        else:
            raise RuntimeError(f"Unknown method: {method}")

        axis_obj = axes.add(inp)
        return {"created": True, "name": axis_obj.name, "method": method}

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    def create_component(self, name: str, parent_name: str = None):
        root = self._root()
        parent = self._component_by_name(parent_name) if parent_name else root

        occ = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        occ.component.name = name
        return {"created": True, "name": name}

    def add_joint(
        self, component_one: str, component_two: str, joint_type: str = "rigid"
    ):
        root = self._root()

        occ1 = occ2 = None
        for occ in root.allOccurrences:
            if occ.component.name == component_one:
                occ1 = occ
            if occ.component.name == component_two:
                occ2 = occ

        if not occ1 or not occ2:
            raise RuntimeError("One or both components not found")

        joints = root.joints
        joint_types = {
            "rigid": adsk.fusion.JointTypes.RigidJointType,
            "revolute": adsk.fusion.JointTypes.RevoluteJointType,
            "slider": adsk.fusion.JointTypes.SliderJointType,
            "cylindrical": adsk.fusion.JointTypes.CylindricalJointType,
            "pin_slot": adsk.fusion.JointTypes.PinSlotJointType,
            "planar": adsk.fusion.JointTypes.PlanarJointType,
            "ball": adsk.fusion.JointTypes.BallJointType,
        }

        jt = joint_types.get(joint_type)
        if jt is None:
            raise RuntimeError(f"Unknown joint type: {joint_type}")

        # Create joint geometry from origin points
        origin1 = occ1.component.originConstructionPoint
        origin2 = occ2.component.originConstructionPoint
        geo1 = adsk.fusion.JointGeometry.createByPoint(occ1, origin1)
        geo2 = adsk.fusion.JointGeometry.createByPoint(occ2, origin2)

        inp = joints.createInput(geo1, geo2)
        if joint_type == "rigid":
            inp.setAsRigidJointMotion()
        joints.add(inp)
        return {"created": True, "joint_type": joint_type}

    def create_as_built_joint(
        self, component_one: str, component_two: str, joint_type: str = "rigid"
    ):
        root = self._root()

        occ1 = occ2 = None
        for occ in root.allOccurrences:
            if occ.component.name == component_one:
                occ1 = occ
            if occ.component.name == component_two:
                occ2 = occ

        if not occ1 or not occ2:
            raise RuntimeError("One or both components not found")

        as_built = root.asBuiltJoints
        inp = as_built.createInput(occ1, occ2, None)
        as_built.add(inp)
        return {"created": True, "joint_type": joint_type}

    def create_rigid_group(self, component_names: list, include_children: bool = True):
        root = self._root()
        occs = adsk.core.ObjectCollection.create()

        for name in component_names:
            for occ in root.allOccurrences:
                if occ.component.name == name:
                    occs.add(occ)
                    break

        if occs.count < 2:
            raise RuntimeError("Need at least 2 components for rigid group")

        groups = root.rigidGroups
        groups.add(occs, include_children)
        return {"created": True, "component_count": occs.count}

    # ------------------------------------------------------------------
    # Inspection / Analysis
    # ------------------------------------------------------------------

    def measure_distance(self, entity_one: str, entity_two: str):
        root = self._root()

        def get_entity(name):
            # Try as body
            for i in range(root.bRepBodies.count):
                b = root.bRepBodies.item(i)
                if b.name == name:
                    return b
            # Try as point (x,y,z format)
            if "," in name:
                coords = [float(x.strip()) for x in name.split(",")]
                return adsk.core.Point3D.create(*coords)
            raise RuntimeError(f"Entity '{name}' not found")

        e1 = get_entity(entity_one)
        e2 = get_entity(entity_two)

        measure = self.app.measureManager
        result = measure.measureMinimumDistance(e1, e2)
        return {
            "distance": result.value,
            "point_one": [
                result.pointOnEntityOne.x,
                result.pointOnEntityOne.y,
                result.pointOnEntityOne.z,
            ],
            "point_two": [
                result.pointOnEntityTwo.x,
                result.pointOnEntityTwo.y,
                result.pointOnEntityTwo.z,
            ],
        }

    def measure_angle(self, entity_one: str, entity_two: str):
        root = self._root()

        def get_entity(name):
            for i in range(root.bRepBodies.count):
                b = root.bRepBodies.item(i)
                if b.name == name:
                    return b.faces.item(0)  # First face
            raise RuntimeError(f"Entity '{name}' not found")

        e1 = get_entity(entity_one)
        e2 = get_entity(entity_two)

        measure = self.app.measureManager
        result = measure.measureAngle(e1, e2)
        return {"angle_degrees": math.degrees(result.value)}

    def get_physical_properties(self, body_name: str, accuracy: str = "medium"):
        body = self._body_by_name(body_name)

        accuracy_map = {
            "low": adsk.fusion.CalculationAccuracy.LowCalculationAccuracy,
            "medium": adsk.fusion.CalculationAccuracy.MediumCalculationAccuracy,
            "high": adsk.fusion.CalculationAccuracy.HighCalculationAccuracy,
            "very_high": adsk.fusion.CalculationAccuracy.VeryHighCalculationAccuracy,
        }
        acc = accuracy_map.get(accuracy, accuracy_map["medium"])

        props = body.getPhysicalProperties(acc)
        return {
            "mass": props.mass,
            "volume": props.volume,
            "area": props.area,
            "density": props.density,
            "center_of_mass": [
                props.centerOfMass.x,
                props.centerOfMass.y,
                props.centerOfMass.z,
            ],
        }

    def create_section_analysis(self, plane: str = "yz", offset: float = 0):
        root = self._root()
        analyses = root.analyses

        inp = analyses.createInput()
        inp.plane = self._construction_plane(plane)
        if offset != 0:
            inp.distance = adsk.core.ValueInput.createByReal(offset)

        analyses.add(inp)
        return {"created": True, "plane": plane, "offset": offset}

    def check_interference(
        self, component_names: list, include_coincident_faces: bool = False
    ):
        root = self._root()
        bodies = adsk.core.ObjectCollection.create()

        for name in component_names:
            for occ in root.allOccurrences:
                if occ.component.name == name:
                    for b in occ.bRepBodies:
                        bodies.add(b)

        if bodies.count < 2:
            raise RuntimeError("Need at least 2 components with bodies")

        interference = root.interfere(bodies, include_coincident_faces)
        results = []
        for i in range(interference.interferenceResultCount):
            result = interference.interferenceResult(i)
            results.append(
                {
                    "body_one": result.entityOne.name,
                    "body_two": result.entityTwo.name,
                    "volume": result.interferenceBody.volume,
                }
            )

        return {"interferences": results, "count": len(results)}

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------

    def set_appearance(
        self,
        target_name: str,
        appearance_name: str,
        target_type: str = "body",
        face_index: int = None,
    ):
        # Find appearance in library — try both known library names
        app_lib = self.app.materialLibraries.itemByName("Fusion Appearance Library")
        if app_lib is None:
            app_lib = self.app.materialLibraries.itemByName(
                "Fusion 360 Appearance Library"
            )
        if app_lib is None:
            # Fall back to searching all libraries
            for i in range(self.app.materialLibraries.count):
                lib = self.app.materialLibraries.item(i)
                if lib.appearances.count > 0:
                    app_lib = lib
                    break
        if app_lib is None:
            raise RuntimeError("No appearance library found")

        appearance = None
        for i in range(app_lib.appearances.count):
            app = app_lib.appearances.item(i)
            if app.name == appearance_name:
                appearance = app
                break

        if not appearance:
            raise RuntimeError(f"Appearance '{appearance_name}' not found")

        if target_type == "body":
            body = self._body_by_name(target_name)
            body.appearance = appearance
        elif target_type == "component":
            comp = self._component_by_name(target_name)
            comp.appearance = appearance
        elif target_type == "face":
            body = self._body_by_name(target_name)
            face = body.faces.item(face_index)
            face.appearance = appearance

        return {"applied": True, "target": target_name, "appearance": appearance_name}

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def get_parameters(self):
        design = self._design()
        params = []
        for param in design.userParameters:
            params.append(
                {
                    "name": param.name,
                    "value": param.value,
                    "expression": param.expression,
                    "unit": param.unit,
                    "comment": param.comment,
                }
            )
        return {"parameters": params, "count": len(params)}

    def create_parameter(self, name: str, value: float, unit: str, comment: str = None):
        design = self._design()
        params = design.userParameters
        params.add(name, adsk.core.ValueInput.createByReal(value), unit, comment or "")
        return {"created": True, "name": name, "value": value, "unit": unit}

    def set_parameter(self, name: str, value: float):
        design = self._design()
        param = design.userParameters.itemByName(name)
        if not param:
            raise RuntimeError(f"Parameter '{name}' not found")
        param.value = value
        return {"updated": True, "name": name, "value": value}

    def delete_parameter(self, name: str):
        design = self._design()
        param = design.userParameters.itemByName(name)
        if not param:
            raise RuntimeError(f"Parameter '{name}' not found")
        param.deleteMe()
        return {"deleted": True, "name": name}

    # ------------------------------------------------------------------
    # Surface Operations
    # ------------------------------------------------------------------

    def patch_surface(
        self, sketch_name: str, profile_index: int = 0, continuity: str = "connected"
    ):
        root = self._root()
        sketch = self._sketch_by_name(sketch_name)

        if sketch.profiles.count == 0:
            raise RuntimeError("No profiles in sketch")
        profile = sketch.profiles.item(profile_index)

        patches = root.features.patchFeatures
        inp = patches.createInput(
            profile, adsk.fusion.FeatureOperations.NewBodyFeatureOperation
        )

        sct = adsk.fusion.SurfaceContinuityTypes
        cont_map = {
            "connected": sct.ConnectedSurfaceContinuityType,
            "tangent": sct.TangentSurfaceContinuityType,
            "curvature": sct.CurvatureSurfaceContinuityType,
        }
        inp.boundaryContinuity = cont_map.get(continuity, cont_map["connected"])

        feat = patches.add(inp)
        return {"feature_name": feat.name, "continuity": continuity}

    def stitch_surfaces(self, body_names: list, tolerance: float = 0.01):
        root = self._root()
        bodies = adsk.core.ObjectCollection.create()
        for name in body_names:
            bodies.add(self._body_by_name(name))

        stitches = root.features.stitchFeatures
        inp = stitches.createInput(
            bodies,
            adsk.core.ValueInput.createByReal(tolerance),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        feat = stitches.add(inp)
        return {"feature_name": feat.name, "body_count": len(body_names)}

    def thicken_surface(
        self, body_name: str, thickness: float, direction: str = "symmetric"
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        faces = adsk.core.ObjectCollection.create()
        for face in body.faces:
            faces.add(face)

        thickens = root.features.thickenFeatures
        inp = thickens.createInput(
            faces,
            adsk.core.ValueInput.createByReal(thickness),
            False,
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
            direction == "symmetric",
        )
        feat = thickens.add(inp)
        return {"feature_name": feat.name, "thickness": thickness}

    def ruled_surface(
        self,
        body_name: str,
        edge_index: int,
        distance: float = 1.0,
        rule_type: str = "normal",
    ):
        root = self._root()
        body = self._body_by_name(body_name)
        edge = body.edges.item(edge_index)

        ruled = root.features.ruledSurfaceFeatures
        inp = ruled.createInput(edge, adsk.core.ValueInput.createByReal(distance))
        feat = ruled.add(inp)
        return {"feature_name": feat.name, "distance": distance}

    def trim_surface(self, body_name: str, tool_name: str):
        root = self._root()
        body = self._body_by_name(body_name)
        tool = self._body_by_name(tool_name)

        trims = root.features.trimFeatures
        inp = trims.createInput(body, tool)
        feat = trims.add(inp)
        return {"feature_name": feat.name}

    # ------------------------------------------------------------------
    # Sheet Metal
    # ------------------------------------------------------------------

    def create_flange(
        self,
        body_name: str,
        edge_index: int,
        height: float = 1.0,
        angle: float = 90,
        bend_radius: float = None,
    ):
        root = self._root()
        body = self._body_by_name(body_name)
        edge = body.edges.item(edge_index)

        flanges = root.features.flangeFeatures
        inp = flanges.createInput(edge, True)
        inp.angle = adsk.core.ValueInput.createByString(f"{angle} deg")
        inp.height = adsk.core.ValueInput.createByReal(height)
        if bend_radius:
            inp.bendRadius = adsk.core.ValueInput.createByReal(bend_radius)

        feat = flanges.add(inp)
        return {"feature_name": feat.name, "height": height, "angle": angle}

    def create_bend(
        self,
        body_name: str,
        bend_line_sketch: str = None,
        angle: float = 90,
        bend_radius: float = None,
    ):
        root = self._root()
        body = self._body_by_name(body_name)

        if bend_line_sketch:
            sketch = self._sketch_by_name(bend_line_sketch)
            bend_line = sketch.sketchCurves.sketchLines.item(0)

            bends = root.features.bendFeatures
            inp = bends.createInput(body, bend_line, True)
            inp.bendAngle = adsk.core.ValueInput.createByString(f"{angle} deg")
            if bend_radius:
                inp.bendRadius = adsk.core.ValueInput.createByReal(bend_radius)

            feat = bends.add(inp)
            return {"feature_name": feat.name, "angle": angle}
        else:
            raise RuntimeError("bend_line_sketch is required")

    def flat_pattern(self, body_name: str):
        root = self._root()
        body = self._body_by_name(body_name)

        flat_patterns = root.features.flatPatternFeatures
        inp = flat_patterns.createInput(body)
        feat = flat_patterns.add(inp)
        return {"feature_name": feat.name}

    def unfold(self, body_name: str, bend_indices: list = None):
        root = self._root()
        body = self._body_by_name(body_name)

        unfolds = root.features.unfoldFeatures

        bends = adsk.core.ObjectCollection.create()
        if bend_indices:
            for idx in bend_indices:
                # Get bend faces from sheet metal body
                bends.add(body.faces.item(idx))
        else:
            # Unfold all bends
            for face in body.faces:
                bends.add(face)

        # Find stationary face (first planar face)
        stationary = None
        for face in body.faces:
            if face.geometry.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType:
                stationary = face
                break

        if not stationary:
            raise RuntimeError("No planar face found for stationary face")

        inp = unfolds.createInput(bends, stationary)
        feat = unfolds.add(inp)
        return {"feature_name": feat.name}

    # ------------------------------------------------------------------
    # CAM
    # ------------------------------------------------------------------

    def _get_cam(self):
        """Get the CAM product from the active document."""
        doc = self.app.activeDocument
        cam_product = doc.products.itemByProductType("CAMProductType")
        if not cam_product:
            raise RuntimeError(
                "No CAM workspace found. Open the Manufacturing workspace "
                "in Fusion 360 at least once to initialise it."
            )
        return cam_product

    def _find_setup(self, cam, name: str):
        for i in range(cam.setups.count):
            s = cam.setups.item(i)
            if s.name == name:
                return s
        raise RuntimeError(f"Setup '{name}' not found")

    def _find_operation(self, setup, name: str):
        for i in range(setup.operations.count):
            op = setup.operations.item(i)
            if op.name == name:
                return op
        raise RuntimeError(f"Operation '{name}' not found in setup '{setup.name}'")

    def cam_list_setups(self):
        cam = self._get_cam()
        result = []
        for i in range(cam.setups.count):
            setup = cam.setups.item(i)
            ops = []
            for j in range(setup.operations.count):
                ops.append(setup.operations.item(j).name)
            result.append(
                {
                    "name": setup.name,
                    "operations": ops,
                    "is_valid": setup.isValid,
                }
            )
        return {"setups": result, "count": len(result)}

    def cam_list_operations(self, setup_name: str):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        result = []
        for i in range(setup.operations.count):
            op = setup.operations.item(i)
            result.append(
                {
                    "name": op.name,
                    "has_toolpath": op.hasToolpath,
                    "is_valid": op.isValid,
                }
            )
        return {"setup": setup_name, "operations": result, "count": len(result)}

    def cam_get_operation_info(self, setup_name: str, operation_name: str):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        op = self._find_operation(setup, operation_name)

        info = {
            "name": op.name,
            "is_valid": op.isValid,
            "has_toolpath": op.hasToolpath,
        }

        if hasattr(op, "tool") and op.tool:
            tool = op.tool
            desc = tool.description if hasattr(tool, "description") else str(tool)
            info["tool"] = {"description": desc}

        if hasattr(op, "parameters"):
            params = {}
            for param in op.parameters:
                try:
                    params[param.name] = param.expression
                except Exception:
                    pass
            info["parameters"] = params

        return info

    def cam_create_setup(
        self,
        body_name: str,
        name: str = None,
        operation_type: str = "milling",
        stock_mode: str = "relative_box",
        stock_offset_sides: float = 0,
        stock_offset_top: float = 0,
        stock_offset_bottom: float = 0,
    ):
        cam = self._get_cam()

        # Warn if a setup with this name already exists — prevents silent duplicates
        if name:
            for i in range(cam.setups.count):
                try:
                    if cam.setups.item(i).name == name:
                        existing = cam.setups.item(i)
                        ops = [op.name for op in self._safe_cam_iter(existing.allOperations)]
                        raise RuntimeError(
                            f"A setup named '{name}' already exists with "
                            f"{len(ops)} operation(s): {ops}. "
                            "Use cam_delete_setup to remove it first, or choose a different name."
                        )
                except RuntimeError:
                    raise
                except Exception:
                    pass

        body = self._body_by_name(body_name)

        op_type_map = {
            "milling": adsk.cam.OperationTypes.MillingOperation,
            "turning": adsk.cam.OperationTypes.TurningOperation,
            "cutting": adsk.cam.OperationTypes.JetOperation,
        }
        op_type = op_type_map.get(operation_type)
        if op_type is None:
            raise RuntimeError(
                f"Unknown operation_type '{operation_type}' "
                "— use milling/turning/cutting"
            )

        setup_input = cam.setups.createInput(op_type)
        setup_input.models = [body]

        if name:
            setup_input.name = name

        setup = cam.setups.add(setup_input)
        return {"name": setup.name, "body": body_name, "operation_type": operation_type}

    def cam_delete_setup(self, setup_name: str):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        op_names = [op.name for op in self._safe_cam_iter(setup.operations)]
        setup.deleteMe()
        return {
            "success": True,
            "deleted_setup": setup_name,
            "deleted_operations": op_names,
        }

    def cam_create_operation(
        self,
        setup_name: str,
        strategy: str,
        name: str = None,
        tool_number: int = None,
        tool_diameter_mm: float = None,  # preferred (mm, matches cam_get_tools output)
        tool_diameter: float = None,     # legacy (cm)
        stepdown: float = None,
        stepover: float = None,
        feed_rate: float = None,
        spindle_speed: float = None,
        coolant: str = "flood",
    ):
        _STRATEGY_MAP = {
            "face":          "face",
            "2d_adaptive":   "adaptive2d",
            "2d_contour":    "contour2d",
            "2d_pocket":     "pocket2d",
            "3d_adaptive":   "adaptive",
            "3d_scallop":    "scallop",
            "3d_parallel":   "parallel",
            "drilling":      "drill",
            "bore":          "bore",
            "thread_milling": "thread",
            "slot":          "slot",
            "trace":         "trace",
            "engrave":       "engrave",
            "ramp":          "ramp",
        }
        fusion_strategy = _STRATEGY_MAP.get(strategy, strategy)

        # Normalize tool_diameter_mm (preferred, mm) → tool_diameter (internal, cm)
        if tool_diameter_mm is not None and tool_diameter is None:
            tool_diameter = tool_diameter_mm * 0.1

        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)

        op_input = setup.operations.createInput(fusion_strategy)
        if name:
            op_input.name = name
        # stepover/stepdown are applied after tool assignment so we can use
        # the actual tool diameter to compute strategy-specific defaults.

        op = setup.operations.add(op_input)
        params = op.parameters

        # --- Tool assignment ---
        # Match by tool_number or tool_diameter in the document library.
        # If nothing matches, fall back to first tool; if library is empty,
        # auto-create a flat endmill with the requested diameter.
        tool_lib = cam.documentToolLibrary
        assigned_tool = None

        for i in range(tool_lib.count):
            try:
                t = tool_lib.item(i)
                if tool_number is not None:
                    p = t.parameters.itemByName("tool_number")
                    if p and int(round(p.value.value)) == tool_number:
                        assigned_tool = t
                        break
                elif tool_diameter is not None:
                    p = t.parameters.itemByName("tool_diameter")
                    if p and abs(p.value.value - tool_diameter) < 0.001:
                        assigned_tool = t
                        break
            except Exception:
                continue
        if assigned_tool is None and tool_lib.count > 0:
            assigned_tool = tool_lib.item(0)

        if assigned_tool is None and tool_diameter:
            import json as _json
            dm = tool_diameter * 10  # cm → mm
            tool_json = _json.dumps({
                "type": "flat end mill",
                "description": f"{dm:.4g}mm Flat Endmill",
                "unit": "millimeters",
                "shaft": {"bladeCount": 4},
                "geometry": {
                    "DC": dm, "LCF": dm * 3, "SFDM": dm, "OAL": dm * 10,
                    "shoulderLength": dm * 5, "fluteLength": dm * 3,
                },
                "start-values": {"presets": [{"description": "default",
                    "n": spindle_speed or 10000,
                    "Vf": feed_rate or 1000,
                    "Vf_plunge": (feed_rate or 1000) * 0.3,
                    "Vf_ramp":   (feed_rate or 1000) * 0.5,
                    "stepdown": dm * 0.3, "stepover": dm * 0.18}]},
                "post-process": {"number": 1, "comment": "", "break-control": False,
                                 "length-offset": 1, "diameter-offset": 1},
            })
            try:
                tool_obj = adsk.cam.Tool.createFromJson(tool_json)
                tool_lib.add(tool_obj)
                assigned_tool = tool_lib.item(tool_lib.count - 1)
            except Exception:
                pass

        if assigned_tool is not None:
            try:
                op.tool = assigned_tool
            except Exception:
                pass

        # --- Feeds and speeds ---
        # Strategy-specific defaults (cm/min and RPM) so feedrates are never 0.
        # Values are conservative but realistic for aluminium on a desktop CNC.
        _FEED_DEFAULTS = {
            "face":      (500.0, 10000),   # high stepover facing pass
            "pocket2d":  (300.0,  8000),
            "adaptive2d":(400.0,  8000),
            "contour2d": (300.0,  8000),
            "adaptive":  (400.0,  8000),
            "scallop":   (300.0,  8000),
            "parallel":  (300.0,  8000),
            "drill":     (150.0,  3000),
            "bore":      (100.0,  3000),
            "thread":    (200.0,  2000),
            "slot":      (200.0,  8000),
            "trace":     (300.0,  8000),
            "engrave":   (200.0, 10000),
            "ramp":      (200.0,  8000),
        }
        default_feed, default_rpm = _FEED_DEFAULTS.get(fusion_strategy, (300.0, 8000))
        eff_feed = feed_rate if feed_rate else default_feed
        eff_rpm  = spindle_speed if spindle_speed else default_rpm

        feed_map = {
            "tool_feedCutting":    eff_feed,
            "tool_feedEntry":      eff_feed,
            "tool_feedExit":       eff_feed,
            "tool_feedTransition": eff_feed,
            "tool_feedPlunge":     eff_feed * 0.3,
            "tool_feedRamp":       eff_feed * 0.5,
            "tool_spindleSpeed":   eff_rpm,
            "finishFeedrate":      eff_feed,
        }
        for pname, val in feed_map.items():
            p = params.itemByName(pname)
            if p:
                try:
                    p.value.value = val
                except Exception:
                    pass

        # --- Stepover / stepdown defaults ---
        # Derive tool diameter from assigned tool (cm units) for percentage-based defaults.
        _tool_diam_cm = None
        if assigned_tool:
            try:
                _p = assigned_tool.parameters.itemByName("tool_diameter")
                if _p:
                    _tool_diam_cm = _p.value.value
            except Exception:
                pass
        if _tool_diam_cm is None and tool_diameter:
            _tool_diam_cm = tool_diameter  # already cm

        # Stepover defaults as fraction of tool diameter; facing uses 85% (one wide pass).
        _STEPOVER_FRAC = {
            "face":       0.85,
            "pocket2d":   0.45,
            "adaptive2d": 0.18,  # optimal load style
            "contour2d":  0.45,
            "adaptive":   0.18,
            "scallop":    0.20,
            "parallel":   0.30,
        }
        # Stepdown defaults as fraction of tool diameter.
        _STEPDOWN_FRAC = {
            "face":       0.30,
            "pocket2d":   0.30,
            "adaptive2d": 0.30,
            "contour2d":  0.50,
            "adaptive":   0.30,
            "scallop":    0.10,
            "parallel":   0.20,
        }

        if not stepover and _tool_diam_cm and fusion_strategy in _STEPOVER_FRAC:
            eff_stepover = _tool_diam_cm * _STEPOVER_FRAC[fusion_strategy]
            for pname in ("stepover", "maximumStepover", "optimalLoad"):
                p = params.itemByName(pname)
                if p:
                    try:
                        p.value.value = eff_stepover
                    except Exception:
                        pass
        elif stepover:
            for pname in ("stepover", "maximumStepover", "optimalLoad"):
                p = params.itemByName(pname)
                if p:
                    try:
                        p.value.value = stepover
                    except Exception:
                        pass

        if not stepdown and _tool_diam_cm and fusion_strategy in _STEPDOWN_FRAC:
            eff_stepdown = _tool_diam_cm * _STEPDOWN_FRAC[fusion_strategy]
            for pname in ("stepdown", "maximumStepdown"):
                p = params.itemByName(pname)
                if p:
                    try:
                        p.value.value = eff_stepdown
                    except Exception:
                        pass
        elif stepdown:
            for pname in ("stepdown", "maximumStepdown"):
                p = params.itemByName(pname)
                if p:
                    try:
                        p.value.value = stepdown
                    except Exception:
                        pass

        # --- Geometry auto-detection ---
        # pocket2d / adaptive2d: set 'pockets' chain to floor faces of any
        # recesses found in the setup model body, then use 'from contour' for
        # bottomHeight so the depth matches the pocket floor automatically.
        # contour2d: set 'contours' chain to the outer loop of the top face,
        # then use 'from surface bottom' so the full body height is milled.
        _POCKET_STRATEGIES  = {"pocket2d", "adaptive2d"}
        _CONTOUR_STRATEGIES = {"contour2d"}

        auto_geometry_ok = False
        auto_geometry_note = None

        if fusion_strategy in _POCKET_STRATEGIES or fusion_strategy in _CONTOUR_STRATEGIES:
            try:
                if setup.models.count > 0:
                    model_body = setup.models.item(0)
                    top_z = model_body.boundingBox.maxPoint.z

                    if fusion_strategy in _POCKET_STRATEGIES:
                        # Collect all upward-facing faces below the model top
                        floor_faces = []
                        for face in model_body.faces:
                            try:
                                n = face.evaluator.getNormalAtPoint(face.pointOnFace)[1]
                                if abs(n.z - 1.0) < 0.01:
                                    if abs(face.boundingBox.minPoint.z - top_z) > 0.01:
                                        floor_faces.append(face)
                            except Exception:
                                continue

                        if floor_faces:
                            # Use the shallowest pocket floor (highest Z)
                            floor_face = max(floor_faces, key=lambda f: f.boundingBox.minPoint.z)
                            floor_edges = list(floor_face.edges)

                            p_geom = params.itemByName("pockets")
                            if p_geom:
                                cv = adsk.cam.CadContours2dParameterValue.cast(p_geom.value)
                                css = cv.getCurveSelections()
                                chain = css.createNewChainSelection()
                                chain.inputGeometry = floor_edges
                                cv.applyCurveSelections(css)

                            p_bh = params.itemByName("bottomHeight_mode")
                            if p_bh:
                                p_bh.value.value = "from contour"
                            p_bho = params.itemByName("bottomHeight_offset")
                            if p_bho:
                                p_bho.value.value = 0.0
                            auto_geometry_ok = True
                        else:
                            # Solid body — no recessed floor faces found.
                            # Adaptive/pocket strategies need pocket geometry to generate
                            # a toolpath; without it Fusion will produce no output.
                            auto_geometry_note = (
                                "Auto-geometry failed: no pocket floor faces found. "
                                "The body appears to be solid (no recesses). "
                                "2D adaptive/pocket strategies require a body with recessed pockets. "
                                "For a solid block, use the 'face' strategy instead to machine the top surface. "
                                "If pockets are intended, add them to the model first, then recreate this operation."
                            )

                    elif fusion_strategy in _CONTOUR_STRATEGIES:
                        # Top face = upward normal at max Z
                        top_face = None
                        for face in model_body.faces:
                            try:
                                n = face.evaluator.getNormalAtPoint(face.pointOnFace)[1]
                                if (abs(n.z - 1.0) < 0.01 and
                                        abs(face.boundingBox.minPoint.z - top_z) < 0.01):
                                    top_face = face
                                    break
                            except Exception:
                                continue

                        if top_face:
                            outer_loop = max(
                                top_face.loops,
                                key=lambda l: sum(e.length for e in l.edges),
                            )
                            outer_edges = list(outer_loop.edges)

                            p_geom = params.itemByName("contours")
                            if p_geom:
                                cv = adsk.cam.CadContours2dParameterValue.cast(p_geom.value)
                                css = cv.getCurveSelections()
                                chain = css.createNewChainSelection()
                                chain.inputGeometry = outer_edges
                                cv.applyCurveSelections(css)

                            p_bh = params.itemByName("bottomHeight_mode")
                            if p_bh:
                                p_bh.value.value = "from surface bottom"
                            p_bho = params.itemByName("bottomHeight_offset")
                            if p_bho:
                                p_bho.value.value = 0.0
                            auto_geometry_ok = True
                        else:
                            auto_geometry_note = (
                                "Auto-geometry failed: could not find top face. "
                                "Call cam_set_operation_geometry to assign geometry manually."
                            )
            except Exception:
                pass  # geometry failure is non-fatal; caller can set manually

        # Toggle a boolean to flush Fusion's cached error state after all changes
        p_md = params.itemByName("doMultipleDepths")
        if p_md:
            try:
                orig = p_md.value.value
                p_md.value.value = not orig
                p_md.value.value = orig
            except Exception:
                pass

        tool_desc = None
        if assigned_tool:
            try:
                tool_desc = assigned_tool.description
            except Exception:
                pass

        result = {
            "name": op.name,
            "setup": setup_name,
            "strategy": strategy,
            "tool_assigned": tool_desc,
            "has_error": self._safe_cam_attr(op, "hasError", None),
        }
        if auto_geometry_note:
            result["geometry_warning"] = auto_geometry_note
        return result

    def cam_generate_toolpath(
        self,
        setup_name: str = None,
        operation_name: str = None,
        generate_all: bool = False,
        timeout_seconds: int = 120,
    ):
        cam = self._get_cam()

        def _poll_future(future, scope_label):
            """Poll GenerateToolpathFuture until done or timeout."""
            import time as _time
            deadline = _time.monotonic() + timeout_seconds
            while not future.isGenerationCompleted:
                if _time.monotonic() > deadline:
                    return {
                        "completed": False,
                        "timed_out": True,
                        "note": (
                            f"Generation still running after {timeout_seconds}s. "
                            "Call cam_get_toolpath_status to check later."
                        ),
                    }
                adsk.doEvents()
                _time.sleep(0.5)
            # Try to surface any error message from the future
            error_msg = None
            for attr in ("error", "errorMessage", "message", "errorDescription"):
                try:
                    v = getattr(future, attr, None)
                    if v:
                        error_msg = str(v)
                        break
                except Exception:
                    pass
            out = {"completed": True, "timed_out": False}
            if error_msg:
                out["generation_error"] = error_msg
            return out

        if generate_all:
            future = cam.generateAllToolpaths(False)
            poll = _poll_future(future, "all")
            return {"scope": "all", **poll}

        if operation_name and setup_name:
            setup = self._find_setup(cam, setup_name)
            op = self._find_operation_all(setup, operation_name)
            ops = adsk.core.ObjectCollection.create()
            ops.add(op)
            future = cam.generateToolpath(ops)
            poll = _poll_future(future, operation_name)
            has_tp = self._safe_cam_attr(op, "hasToolpath", False)
            valid = self._safe_cam_attr(op, "isToolpathValid", False) if has_tp else False
            result = {
                "scope": "operation",
                "operation": operation_name,
                "has_toolpath": has_tp,
                "is_valid": valid,
                **poll,
            }
            if not has_tp:
                result["note"] = (
                    "Toolpath generation completed but produced no output. "
                    "Common causes: (1) geometry not assigned — call cam_set_operation_geometry first; "
                    "(2) strategy incompatible with model — 2D adaptive/pocket require a body with recesses, "
                    "use 'face' strategy for solid blocks; "
                    "(3) operation has an error — check has_error in cam_get_operation_details."
                )
            return result

        if setup_name:
            setup = self._find_setup(cam, setup_name)
            ops = adsk.core.ObjectCollection.create()
            for i in range(setup.operations.count):
                ops.add(setup.operations.item(i))
            future = cam.generateToolpath(ops)
            poll = _poll_future(future, setup_name)
            return {"scope": "setup", "setup": setup_name, **poll}

        raise RuntimeError("Provide setup_name, operation_name, or generate_all=true")

    def cam_post_process(
        self,
        setup_name: str,
        operation_name: str = None,
        post_processor: str = "fanuc",
        output_folder: str = None,
        output_units: str = "mm",
        program_number: int = 1,
    ):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)

        if not output_folder:
            output_folder = os.path.join(os.path.expanduser("~"), "Desktop")

        post_config = os.path.join(cam.genericPostFolder, f"{post_processor}.cps")
        if not os.path.exists(post_config):
            # genericPostFolder is a cache dir that may be empty; search the
            # webdeploy Posts directory for the .cps file.
            import glob as _glob
            webdeploy = os.path.join(
                os.path.expanduser("~"),
                "AppData", "Local", "Autodesk", "webdeploy", "production",
            )
            matches = _glob.glob(
                os.path.join(webdeploy, "**", f"{post_processor}.cps"),
                recursive=True,
            )
            if matches:
                post_config = matches[0]
            else:
                raise RuntimeError(
                    f"Post processor '{post_processor}.cps' not found in "
                    f"{cam.genericPostFolder} or {webdeploy}. "
                    f"Available: {[os.path.basename(p) for p in _glob.glob(os.path.join(webdeploy, '**', '*.cps'), recursive=True)[:10]]}"
                )

        units = (
            adsk.cam.PostOutputUnitOptions.MillimetersOutput
            if output_units == "mm"
            else adsk.cam.PostOutputUnitOptions.InchesOutput
        )

        post_input = adsk.cam.PostProcessInput.create(
            str(program_number), post_config, output_folder, units
        )
        post_input.isOpenInEditor = False

        if operation_name:
            op = self._find_operation(setup, operation_name)
            has_tp = self._safe_cam_attr(op, "hasToolpath", False)
            valid = self._safe_cam_attr(op, "isToolpathValid", False) if has_tp else False
            if not valid:
                raise RuntimeError(
                    f"Operation '{operation_name}' has no valid toolpath. "
                    "Call cam_generate_toolpath first and confirm is_valid: true "
                    "before post-processing."
                )
            cam.postProcess(op, post_input)
        else:
            # Check at least one valid toolpath exists in the setup
            any_valid = any(
                self._safe_cam_attr(op, "isToolpathValid", False)
                for op in self._safe_cam_iter(setup.allOperations)
                if self._safe_cam_attr(op, "hasToolpath", False)
            )
            if not any_valid:
                raise RuntimeError(
                    f"Setup '{setup_name}' has no valid toolpaths. "
                    "Call cam_generate_toolpath first and confirm is_valid: true "
                    "before post-processing."
                )
            cam.postProcess(setup, post_input)

        output_file = os.path.join(output_folder, f"{program_number}.nc")
        return {
            "setup": setup_name,
            "post_processor": post_processor,
            "output_folder": output_folder,
            "output_file": output_file,
            "units": output_units,
        }

    # ------------------------------------------------------------------
    # CAM — extended query / write tools (Step 2)
    # ------------------------------------------------------------------

    # ── private helpers ────────────────────────────────────────────────

    @staticmethod
    def _safe_cam_attr(obj, attr, default=None):
        """Return obj.attr, or *default* if the attribute is missing or raises."""
        try:
            val = getattr(obj, attr, default)
            return default if val is None else val
        except Exception:
            return default

    @staticmethod
    def _safe_cam_iter(collection):
        """Yield items from a Fusion collection, skipping any that raise."""
        try:
            count = collection.count
        except Exception:
            return
        for i in range(count):
            try:
                yield collection.item(i)
            except Exception:
                continue

    @staticmethod
    def _read_cam_param(params_obj, name):
        """Read a CAM ParameterList entry; return numeric value or None."""
        try:
            p = params_obj.itemByName(name)
            if p is None:
                return None
            val = p.value
            if hasattr(val, "value"):
                val = val.value
            return val
        except Exception:
            return None

    def _find_operation_all(self, setup, name: str):
        """Find operation by name using allOperations (includes folder-nested ops)."""
        for op in self._safe_cam_iter(setup.allOperations):
            if op.name == name:
                return op
        raise RuntimeError(f"Operation '{name}' not found in setup '{setup.name}'")

    def _extract_tool_record(self, tool):
        """Return a dict of geometry/type info for a tool object."""
        info = {}
        try:
            tp = tool.parameters
            for key in (
                "tool_number", "tool_diameter", "tool_fluteLength",
                "tool_shoulderLength", "tool_overallLength",
                "tool_numberOfFlutes", "tool_cornerRadius",
            ):
                val = self._read_cam_param(tp, key)
                if val is not None:
                    if key in ("tool_diameter", "tool_fluteLength",
                               "tool_shoulderLength", "tool_overallLength",
                               "tool_cornerRadius"):
                        val = round(val * 10, 4)  # cm → mm
                    info[key] = val
        except Exception:
            pass
        try:
            info["tool_type"] = str(tool.type)
        except Exception:
            pass
        try:
            info["description"] = tool.description
        except Exception:
            pass
        return info

    # ── 1. cam_get_toolpath_status ─────────────────────────────────────

    def cam_get_toolpath_status(self, setup_name: str = None, operation_name: str = None):
        cam = self._get_cam()

        if setup_name:
            setups = [self._find_setup(cam, setup_name)]
        else:
            setups = list(self._safe_cam_iter(cam.setups))

        result = []
        for setup in setups:
            ops = []
            for op in self._safe_cam_iter(setup.allOperations):
                if operation_name and op.name != operation_name:
                    continue
                has_tp = self._safe_cam_attr(op, "hasToolpath", False)
                valid = self._safe_cam_attr(op, "isToolpathValid", False) if has_tp else False
                ops.append({
                    "name": op.name,
                    "is_suppressed": self._safe_cam_attr(op, "isSuppressed", False),
                    "has_toolpath": has_tp,
                    "is_valid": valid,
                    "is_outdated": has_tp and not valid,
                })
            result.append({"name": setup.name, "operations": ops})

        total = sum(len(s["operations"]) for s in result)
        with_tp = sum(
            1 for s in result for o in s["operations"] if o["has_toolpath"]
        )
        valid_count = sum(
            1 for s in result for o in s["operations"] if o["is_valid"]
        )
        return {
            "setups": result,
            "summary": {
                "total_operations": total,
                "with_toolpath": with_tp,
                "valid": valid_count,
                "outdated": with_tp - valid_count,
            },
        }

    # ── 2. cam_get_operation_details ───────────────────────────────────

    def cam_get_operation_details(self, setup_name: str, operation_name: str):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        op = self._find_operation_all(setup, operation_name)
        op_params = op.parameters

        # Tool
        tool_info = {}
        try:
            tool = op.tool
            if tool:
                tool_info = self._extract_tool_record(tool)
        except Exception:
            pass

        # Parameters — return using the same user-friendly keys accepted by
        # cam_update_operation_parameters so Qwen can read then write without
        # mapping internal names. Values already converted to display units.
        params_out = {}
        for user_key, (fusion_key, unit, scale) in self._OP_PARAM_MAP.items():
            val = self._read_cam_param(op_params, fusion_key)
            if val is not None:
                params_out[user_key] = round(val / scale, 4)

        # tool_spindleDirection is read-only metadata not in _OP_PARAM_MAP
        sd = self._read_cam_param(op_params, "tool_spindleDirection")
        if sd is not None:
            params_out["spindle_direction"] = sd

        coolant = self._read_cam_param(op_params, "tool_coolant")
        if coolant is not None:
            params_out["coolant"] = coolant

        # Auto-capture is intentionally omitted — iterating all CAM params
        # is too expensive and causes TCP bridge timeouts.

        has_tp = self._safe_cam_attr(op, "hasToolpath", False)
        valid = self._safe_cam_attr(op, "isToolpathValid", False) if has_tp else False

        return {
            "name": op.name,
            "strategy": str(op.strategy) if hasattr(op, "strategy") else None,
            "is_suppressed": self._safe_cam_attr(op, "isSuppressed", False),
            "has_toolpath": has_tp,
            "is_valid": valid,
            "tool": tool_info,
            "parameters": params_out,
        }

    # ── 3. cam_update_operation_parameters ────────────────────────────

    # Maps user-friendly keys → (fusion_param_name, display_unit, to_internal_scale)
    # CAM operation params use .value.value in Fusion internal units (cm, cm/min, rpm).
    # to_internal_scale: multiply user display value by this to get internal value.
    #   mm → cm:         scale = 0.1
    #   mm/min → cm/min: scale = 0.1
    #   rpm → rpm:       scale = 1.0
    _OP_PARAM_MAP = {
        "tool_diameter_mm":       ("tool_diameter",           "mm",     0.1),
        "cutting_feedrate_mmpm":  ("tool_feedCutting",        "mm/min", 0.1),
        "entry_feedrate_mmpm":    ("tool_feedEntry",          "mm/min", 0.1),
        "exit_feedrate_mmpm":     ("tool_feedExit",           "mm/min", 0.1),
        "plunge_feedrate_mmpm":   ("tool_feedPlunge",         "mm/min", 0.1),
        "ramp_feedrate_mmpm":     ("tool_feedRamp",           "mm/min", 0.1),
        "reduced_feedrate_mmpm":  ("tool_feedReducedCutting", "mm/min", 0.1),
        "spindle_speed_rpm":      ("tool_spindleSpeed",       "rpm",    1.0),
        "stepover_mm":            ("stepover",                "mm",     0.1),
        "optimal_load_mm":        ("optimalLoad",             "mm",     0.1),
        "stepdown_mm":            ("stepdown",                "mm",     0.1),
        "max_stepdown_mm":        ("maximumStepdown",         "mm",     0.1),
        "tolerance_mm":           ("tolerance",               "mm",     0.1),
        "stock_to_leave_mm":      ("stockToLeave",            "mm",     0.1),
        "axial_stock_mm":         ("axialStock",              "mm",     0.1),
    }

    def cam_update_operation_parameters(
        self, setup_name: str, operation_name: str, parameters: dict
    ):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        op = self._find_operation_all(setup, operation_name)
        op_params = op.parameters

        changes = []
        skipped = []
        warnings = []

        for user_key, value in parameters.items():
            if user_key not in self._OP_PARAM_MAP:
                warnings.append(
                    f"Unknown parameter '{user_key}' — valid keys: "
                    + ", ".join(sorted(self._OP_PARAM_MAP))
                )
                skipped.append(user_key)
                continue

            fusion_key, unit, scale = self._OP_PARAM_MAP[user_key]
            try:
                p = op_params.itemByName(fusion_key)
                if p is None:
                    warnings.append(
                        f"'{fusion_key}' not available for this operation type"
                    )
                    skipped.append(user_key)
                    continue
                try:
                    if p.isReadOnly:
                        warnings.append(f"'{fusion_key}' is read-only")
                        skipped.append(user_key)
                        continue
                except Exception:
                    pass

                # Read before value (internal unit → display unit)
                try:
                    before_display = round(p.value.value / scale, 4)
                except Exception:
                    before_display = None

                # Write via .value.value in Fusion internal units (cm, cm/min, rpm)
                # CAM operation params do not support .expression — must set .value.value
                p.value.value = float(value) * scale

                # Read after to confirm
                try:
                    after_display = round(p.value.value / scale, 4)
                except Exception:
                    after_display = float(value)

                changes.append({
                    "parameter": user_key,
                    "fusion_param": fusion_key,
                    "unit": unit,
                    "before": before_display,
                    "after": after_display,
                })
            except Exception as e:
                warnings.append(f"Failed to set '{user_key}': {e}")
                skipped.append(user_key)

        # Flush Fusion's cached error state after changing params
        p_md = op_params.itemByName("doMultipleDepths")
        if p_md:
            try:
                orig = p_md.value.value
                p_md.value.value = not orig
                p_md.value.value = orig
            except Exception:
                pass

        adsk.doEvents()

        return {
            "success": len(changes) > 0,
            "operation": operation_name,
            "changes": changes,
            "changes_applied": len(changes),
            "skipped": skipped,
            "has_error": self._safe_cam_attr(op, "hasError", None),
            "warnings": warnings,
        }

    # ── 4. cam_get_tools ───────────────────────────────────────────────

    def cam_get_tools(self):
        cam = self._get_cam()

        # Build operation cross-reference: (tool_number, description) → [op names]
        # Keying on both fields avoids cross-contamination when all tools share
        # tool_number 0 (Fusion ignores the number field on createFromJson).
        ops_by_tool_key = {}
        for op in self._safe_cam_iter(cam.allOperations):
            if self._safe_cam_attr(op, "isSuppressed"):
                continue
            try:
                tool = op.tool
                if tool:
                    num = self._read_cam_param(tool.parameters, "tool_number")
                    desc = self._safe_cam_attr(tool, "description", "")
                    key = (int(num) if num is not None else -1, desc or "")
                    ops_by_tool_key.setdefault(key, []).append(op.name)
            except Exception:
                pass

        tools_list = []

        # Primary: document tool library
        lib = self._safe_cam_attr(cam, "documentToolLibrary")
        if lib:
            seen_lib = set()
            for tool in self._safe_cam_iter(lib):
                try:
                    info = self._extract_tool_record(tool)
                    num = int(info.get("tool_number", -1))
                    desc = info.get("description", "") or ""
                    key = (num, desc)
                    if key in seen_lib:
                        continue
                    seen_lib.add(key)
                    tools_list.append({
                        "tool": info,
                        "used_in_operations": ops_by_tool_key.get(key, []),
                    })
                except Exception:
                    pass

        # Fallback: collect unique tools from operations
        if not tools_list:
            seen = {}
            for op in self._safe_cam_iter(cam.allOperations):
                if self._safe_cam_attr(op, "isSuppressed"):
                    continue
                try:
                    tool = op.tool
                    if tool:
                        num = self._read_cam_param(tool.parameters, "tool_number")
                        desc = self._safe_cam_attr(tool, "description", "") or ""
                        key = (int(num) if num is not None else -1, desc)
                        if key not in seen:
                            seen[key] = self._extract_tool_record(tool)
                            tools_list.append({
                                "tool": seen[key],
                                "used_in_operations": ops_by_tool_key.get(key, []),
                            })
                except Exception:
                    pass

        return {"tools": tools_list, "count": len(tools_list)}

    # ── 5. cam_get_machining_time ──────────────────────────────────────

    def cam_get_machining_time(self, setup_name: str = None):
        cam = self._get_cam()

        if setup_name:
            setups = [self._find_setup(cam, setup_name)]
        else:
            setups = list(self._safe_cam_iter(cam.setups))

        DEFAULT_RAPID = 500.0   # cm/min
        DEFAULT_TC    = 0.0     # seconds

        results = []
        for setup in setups:
            ops_data = []
            total_time = 0.0

            for op in self._safe_cam_iter(setup.allOperations):
                has_tp = self._safe_cam_attr(op, "hasToolpath", False)
                suppressed = self._safe_cam_attr(op, "isSuppressed", False)
                op_entry = {
                    "name": op.name,
                    "has_toolpath": has_tp,
                    "is_suppressed": suppressed,
                }

                if has_tp and not suppressed:
                    try:
                        tr = cam.getMachiningTime(op, 1.0, DEFAULT_RAPID, DEFAULT_TC)
                        if tr:
                            t = None
                            for attr in ("machiningTime", "totalTime"):
                                t = self._safe_cam_attr(tr, attr)
                                if t is not None:
                                    break
                            if t is not None:
                                op_entry["time_seconds"] = round(t, 2)
                                total_time += t
                    except Exception as e:
                        op_entry["time_error"] = str(e)

                ops_data.append(op_entry)

            results.append({
                "name": setup.name,
                "total_time_seconds": round(total_time, 2),
                "operations": ops_data,
            })

        return {"setups": results}

    # ── 6. cam_get_library_tools ───────────────────────────────────────

    def cam_get_library_tools(self, library_name: str = None):
        cam = self._get_cam()

        # Document library — always available; dedup on (tool_number, description)
        doc_tools = []
        seen_doc = set()
        try:
            for i in range(cam.documentToolLibrary.count):
                try:
                    info = self._extract_tool_record(cam.documentToolLibrary.item(i))
                    key = (int(info.get("tool_number", -1)), info.get("description", "") or "")
                    if key in seen_doc:
                        continue
                    seen_doc.add(key)
                    doc_tools.append(info)
                except Exception:
                    pass
        except Exception:
            pass

        results = []
        if doc_tools:
            results.append({
                "location": "document",
                "url": "document",
                "tool_count": len(doc_tools),
                "tools": doc_tools,
            })

        # External libraries via libraryManager (not available in all API versions)
        try:
            lib_mgr = cam.libraryManager
            tool_libs = lib_mgr.toolLibraries
            loc_map = {
                "local": getattr(adsk.cam.LibraryLocations, "LocalLibraryLocation", None),
                "fusion360": getattr(adsk.cam.LibraryLocations, "Fusion360LibraryLocation", None),
                "cloud": getattr(adsk.cam.LibraryLocations, "CloudLibraryLocation", None),
            }
            for loc, loc_enum in loc_map.items():
                if loc_enum is None:
                    continue
                try:
                    urls = tool_libs.urlsByLocation(loc_enum)
                except Exception:
                    continue
                for i in range(urls.count):
                    try:
                        url = urls.item(i)
                        url_str = str(url)
                        if library_name and library_name.lower() not in url_str.lower():
                            continue
                        lib = tool_libs.toolLibraryAtURL(url)
                        if not lib:
                            continue
                        tools = []
                        seen_ext = set()
                        for tool in self._safe_cam_iter(lib):
                            try:
                                info = self._extract_tool_record(tool)
                                key = (int(info.get("tool_number", -1)), info.get("description", "") or "")
                                if key in seen_ext:
                                    continue
                                seen_ext.add(key)
                                tools.append(info)
                            except Exception:
                                pass
                        results.append({
                            "location": loc,
                            "url": url_str,
                            "tool_count": len(tools),
                            "tools": tools,
                        })
                    except Exception:
                        continue
        except Exception:
            # libraryManager not available in this Fusion API version
            pass

        note = (
            None if results else
            "Document tool library is empty and no external libraries are accessible. "
            "Use cam_create_document_tool to add a tool to the document library."
        )
        out = {"libraries": results, "library_count": len(results)}
        if note:
            out["note"] = note
        return out

    # ── 7. cam_update_setup_machine_params ────────────────────────────

    _MACHINE_PARAM_MAP = {
        "max_spindle_speed_rpm":       ("machine_maxSpindleSpeed",    "rpm"),
        "min_spindle_speed_rpm":       ("machine_minSpindleSpeed",    "rpm"),
        "max_cutting_feedrate_mmpm":   ("machine_maxFeedrate",        "mm/min"),
        "rapid_feedrate_mmpm":         ("machine_rapidFeedrate",      "mm/min"),
        "tool_change_time_s":          ("machine_toolChangetime",     "s"),
    }

    def cam_update_setup_machine_params(
        self, setup_name: str, machine_params: dict
    ):
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        setup_params = setup.parameters

        updated = []
        skipped = []
        warnings = []

        for user_key, value in machine_params.items():
            if user_key not in self._MACHINE_PARAM_MAP:
                warnings.append(
                    f"Unknown param '{user_key}' — valid keys: "
                    + ", ".join(sorted(self._MACHINE_PARAM_MAP))
                )
                skipped.append(user_key)
                continue

            fusion_key, unit = self._MACHINE_PARAM_MAP[user_key]
            try:
                p = setup_params.itemByName(fusion_key)
                if p is None:
                    warnings.append(
                        f"'{fusion_key}' not available on this setup — "
                        "machine parameters only exist when a machine configuration "
                        "is assigned (Setup → Machine tab in Fusion 360)"
                    )
                    skipped.append(user_key)
                    continue
                try:
                    if p.isReadOnly:
                        warnings.append(f"'{fusion_key}' is read-only")
                        skipped.append(user_key)
                        continue
                except Exception:
                    pass
                p.expression = f"{value} {unit}"
                updated.append(user_key)
            except Exception as e:
                warnings.append(f"Failed to set '{user_key}': {e}")
                skipped.append(user_key)

        adsk.doEvents()

        return {
            "success": len(updated) > 0,
            "setup": setup_name,
            "updated": updated,
            "skipped": skipped,
            "warnings": warnings,
        }

    # ── 8. cam_get_nc_programs ─────────────────────────────────────────

    def cam_get_nc_programs(self):
        cam = self._get_cam()

        programs = []
        for nc in self._safe_cam_iter(cam.ncPrograms):
            entry = {
                "name": nc.name,
                "is_suppressed": self._safe_cam_attr(nc, "isSuppressed", False),
            }

            # Operations
            ops = []
            for op in self._safe_cam_iter(nc.operations):
                ops.append(op.name)
            if ops:
                entry["operations"] = ops
                entry["operation_count"] = len(ops)

            # Post-processor
            try:
                pp = nc.postConfiguration
                if pp:
                    pp_info = {}
                    name = self._safe_cam_attr(pp, "name")
                    if name:
                        pp_info["name"] = name
                    desc = self._safe_cam_attr(pp, "description")
                    if desc:
                        pp_info["description"] = desc
                    try:
                        url = pp.url()
                        pp_info["url"] = str(url)
                    except Exception:
                        pass
                    if pp_info:
                        entry["post_processor"] = pp_info
            except Exception:
                pass

            # Output settings
            nc_params = self._safe_cam_attr(nc, "parameters")
            if nc_params:
                settings = {}
                for key in ("nc_program_filename", "nc_program_openInEditor",
                            "nc_program_number"):
                    val = self._read_cam_param(nc_params, key)
                    if val is not None:
                        settings[key] = val
                if settings:
                    entry["settings"] = settings

            programs.append(entry)

        return {"nc_programs": programs, "count": len(programs)}

    # ------------------------------------------------------------------
    # Health check
    # ── 9. cam_set_operation_geometry ─────────────────────────────────

    # Parameter names that hold the machining model geometry, by priority.
    # Different strategies use different param names for the same concept.
    _MODEL_PARAM_NAMES = (
        "pockets",           # 2D Adaptive, 2D Pocket
        "model",             # 3D Adaptive, Scallop, etc.
        "stockContours",     # stock boundary contours
        "machiningBoundary", # some 3D ops
        "silhouette",        # silhouette-based ops
        "tool_frame",        # misc
    )

    def cam_set_operation_geometry(
        self,
        setup_name: str,
        operation_name: str,
        body_index: int = 0,
        body_name: str = None,
        face_indices: list = None,
    ):
        """Assign model geometry (body faces) to a CAM operation's model parameter.

        Selects all faces of the target body by default, equivalent to clicking
        the body in the Fusion UI geometry selection dialog.
        """
        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        op = self._find_operation_all(setup, operation_name)

        # ── Collect bodies from design root component ──────────────────
        # Most CAM setups reference design bodies directly; manufacturing
        # models (cam.manufacturingModels) are an explicit opt-in feature.
        bodies = []
        try:
            doc = self.app.activeDocument
            design_product = None
            for i in range(doc.products.count):
                p = doc.products.item(i)
                if p.productType == "DesignProductType":
                    import adsk.fusion
                    design_product = adsk.fusion.Design.cast(p)
                    break
            if design_product:
                root = design_product.rootComponent
                for i in range(root.bRepBodies.count):
                    bodies.append(root.bRepBodies.item(i))
                # Also search immediate sub-occurrences (assemblies)
                for oi in range(root.occurrences.count):
                    occ = root.occurrences.item(oi)
                    for bi in range(occ.bRepBodies.count):
                        bodies.append(occ.bRepBodies.item(bi))
        except Exception:
            pass

        if not bodies:
            raise RuntimeError(
                "No solid bodies found in the design. "
                "Make sure the design has at least one body."
            )

        # ── Select target body ─────────────────────────────────────────
        target_body = None
        if body_name:
            for b in bodies:
                if b.name == body_name:
                    target_body = b
                    break
            if target_body is None:
                available = [b.name for b in bodies]
                raise RuntimeError(
                    f"Body '{body_name}' not found. Available: {available}"
                )
        else:
            if body_index >= len(bodies):
                raise RuntimeError(
                    f"body_index {body_index} out of range "
                    f"(found {len(bodies)} bodies)"
                )
            target_body = bodies[body_index]

        # ── Collect faces ──────────────────────────────────────────────
        if face_indices is not None:
            if len(face_indices) == 0:
                raise RuntimeError(
                    "face_indices must not be empty. "
                    "Omit face_indices to select all faces, or provide at least one index."
                )
            faces = []
            for fi in face_indices:
                if fi >= target_body.faces.count:
                    raise RuntimeError(
                        f"face_index {fi} out of range "
                        f"(body has {target_body.faces.count} faces)"
                    )
                faces.append(target_body.faces.item(fi))
        else:
            # All faces of the body — equivalent to clicking the body in the UI
            faces = [target_body.faces.item(i) for i in range(target_body.faces.count)]

        # ── Assign to operation geometry parameter ─────────────────────
        # CadObjectParameterValue requires an ObjectCollection, not a plain list.
        face_col = adsk.core.ObjectCollection.create()
        for f in faces:
            face_col.add(f)

        param_used = None
        for param_name in self._MODEL_PARAM_NAMES:
            try:
                p = op.parameters.itemByName(param_name)
                if p is None:
                    continue
                geom = p.value  # CadObjectParameterValue
                if geom is None:
                    continue
                geom.value = face_col
                param_used = param_name
                break
            except Exception:
                continue

        if param_used is None:
            # Enumerate all parameter names on this operation for diagnostics
            all_params = []
            try:
                params_list = op.parameters
                for i in range(params_list.count):
                    try:
                        p = params_list.item(i)
                        all_params.append(p.name)
                    except Exception:
                        pass
            except Exception:
                pass
            raise RuntimeError(
                f"Could not find a writable geometry parameter on operation "
                f"'{operation_name}'. Tried: {list(self._MODEL_PARAM_NAMES)}. "
                f"All available parameters: {all_params}"
            )

        return {
            "success": True,
            "setup": setup_name,
            "operation": operation_name,
            "body": target_body.name,
            "param_used": param_used,
            "face_count": len(faces),
            "note": (
                "Geometry assigned. Toolpath is now outdated — "
                "call cam_generate_toolpath to regenerate."
            ),
        }

    # ── 10. cam_set_operation_tool ────────────────────────────────────

    def cam_set_operation_tool(
        self,
        setup_name: str,
        operation_name: str,
        tool_number: int = None,
        tool_description: str = None,
    ):
        """Assign a tool to a CAM operation, searching all available libraries."""
        if tool_number is None and tool_description is None:
            raise ValueError(
                "Provide at least one of: tool_number, tool_description"
            )

        cam = self._get_cam()
        setup = self._find_setup(cam, setup_name)
        op = self._find_operation_all(setup, operation_name)

        def _tool_number(t):
            # Tool.number may be a direct attr or live in parameters
            try:
                n = t.number
                if n is not None:
                    return int(n)
            except Exception:
                pass
            try:
                n = self._read_cam_param(t.parameters, "tool_number")
                if n is not None:
                    return int(n)
            except Exception:
                pass
            return None

        def _tool_description(t):
            try:
                d = t.description
                if d is not None:
                    return str(d)
            except Exception:
                pass
            return ""

        def _matches(t):
            if tool_number is not None:
                if _tool_number(t) == tool_number:
                    return True
            if tool_description is not None:
                if tool_description.lower() in _tool_description(t).lower():
                    return True
            return False

        matched_tool = None
        library_source = None

        # 1. Document library (fastest, always checked first)
        tool_lib = cam.documentToolLibrary
        for i in range(tool_lib.count):
            try:
                t = tool_lib.item(i)
                if _matches(t):
                    matched_tool = t
                    library_source = "document"
                    break
            except Exception:
                continue

        if matched_tool is None:
            criteria = (
                f"tool_number={tool_number}"
                if tool_number is not None
                else f"tool_description='{tool_description}'"
            )
            raise RuntimeError(
                f"No tool matching {criteria} in the document library "
                f"(library has {tool_lib.count} tools). "
                f"Use cam_create_document_tool to add a tool first."
            )

        # Direct assignment
        try:
            op.tool = matched_tool
        except Exception:
            # Fallback: set tool_number parameter directly
            p = op.parameters.itemByName("tool_number")
            num = _tool_number(matched_tool)
            if p is not None and num is not None:
                p.expression = str(num)
            else:
                raise RuntimeError(
                    "Could not assign tool: op.tool is read-only and "
                    "no 'tool_number' parameter found on the operation."
                )

        tool_record = self._extract_tool_record(matched_tool)

        return {
            "success": True,
            "operation": operation_name,
            "library_source": library_source,
            "tool_assigned": tool_record,
        }

    # ── 11. cam_create_document_tool ─────────────────────────────────

    # Map MCP tool_type strings to Fusion CAM JSON type strings
    _TOOL_TYPE_JSON = {
        "flat_end_mill": "flat end mill",
        "ball_end_mill": "ball end mill",
        "bull_nose":     "bull nose end mill",
        "drill":         "drill",
        "chamfer":       "chamfer mill",
    }

    def cam_create_document_tool(
        self,
        tool_number: int = 1,
        description: str = "Flat End Mill",
        tool_type: str = "flat_end_mill",
        diameter_mm: float = 6.0,
        flute_length_mm: float = 15.0,
        overall_length_mm: float = 50.0,
        number_of_flutes: int = 4,
        corner_radius_mm: float = 0.0,
    ):
        """Create a tool in the document tool library via JSON factory API."""
        import json as _json
        import uuid as _uuid

        cam = self._get_cam()
        tool_lib = cam.documentToolLibrary

        # Dedup: return existing tool if same tool_number OR same type+diameter+flutes.
        # Prevents library pollution across multi-step sessions.
        type_str = self._TOOL_TYPE_JSON.get(tool_type, "flat end mill")
        for i in range(tool_lib.count):
            try:
                existing = tool_lib.item(i)
                existing_num = self._read_cam_param(existing.parameters, "tool_number")
                existing_type = str(existing.type) if hasattr(existing, "type") else ""
                existing_diam_cm = self._read_cam_param(existing.parameters, "tool_diameter")
                existing_flutes = self._read_cam_param(existing.parameters, "tool_numberOfFlutes")

                same_number = (
                    existing_num is not None and int(round(existing_num)) == tool_number
                )
                same_geometry = (
                    existing_type == type_str
                    and existing_diam_cm is not None
                    and abs(existing_diam_cm * 10 - diameter_mm) < 0.01
                    and existing_flutes is not None
                    and int(existing_flutes) == number_of_flutes
                )
                if same_number or same_geometry:
                    return {
                        "success": True,
                        "already_exists": True,
                        "tool_number": existing_num,
                        "description": self._safe_cam_attr(existing, "description", ""),
                        "tool_type": tool_type,
                        "type_str": type_str,
                        "diameter_mm": diameter_mm,
                        "document_library_count": tool_lib.count,
                    }
            except Exception:
                continue
        tool_data = {
            "type": type_str,
            "unit": "millimeters",
            "description": description,
            "guid": "{" + str(_uuid.uuid4()) + "}",
            "product-id": f"mcp-tool-{tool_number}",
            "BMC": "carbide",
            "number": tool_number,
            "post-process": {
                "number": tool_number,
                "comment": "",
                "diameter-offset": tool_number,
                "length-offset": tool_number,
            },
            "DC": diameter_mm,
            "LCF": flute_length_mm,
            "OAL": overall_length_mm,
            "ZEFF": number_of_flutes,
            "NOF": number_of_flutes,
            "RE": corner_radius_mm,
            "APMX": diameter_mm,
            "RPMF": 0,
            "fz": 0.0,
        }

        tool_obj = None
        method_used = None
        errors = []

        # Approach A: Tool.createFromJson (single-tool JSON)
        for factory in (
            getattr(adsk.cam.Tool, "createFromJson", None),
            getattr(adsk.cam, "Tool_createFromJson", None),
        ):
            if factory is None:
                continue
            try:
                t = factory(_json.dumps(tool_data))
                if t is not None:
                    tool_obj = t
                    method_used = "Tool.createFromJson"
                    break
            except Exception as e:
                errors.append(f"Tool.createFromJson: {e}")

        # Approach B: ToolLibrary.createFromJson (full library JSON),
        # then read first tool out and add it to the document library.
        if tool_obj is None:
            lib_json = _json.dumps({"data": [tool_data], "version": 1})
            for factory in (
                getattr(adsk.cam.ToolLibrary, "createFromJson", None),
                getattr(adsk.cam, "ToolLibrary_createFromJson", None),
            ):
                if factory is None:
                    continue
                try:
                    tmp_lib = factory(lib_json)
                    if tmp_lib and tmp_lib.count > 0:
                        tool_obj = tmp_lib.item(0)
                        method_used = "ToolLibrary.createFromJson"
                        break
                except Exception as e:
                    errors.append(f"ToolLibrary.createFromJson: {e}")

        if tool_obj is None:
            raise RuntimeError(
                f"All tool creation approaches failed. Errors: {errors}. "
                f"Document library add() methods: "
                f"{[m for m in dir(tool_lib) if not m.startswith('_')]}"
            )

        # Add to document library
        add_result = tool_lib.add(tool_obj)

        # Fusion ignores the "number" JSON field on createFromJson — try to
        # set tool_number directly on the parameter object after library add.
        actual_number = tool_number
        try:
            added = tool_lib.item(tool_lib.count - 1)
            p = added.parameters.itemByName("tool_number")
            if p is not None:
                p.expression = str(tool_number)
            actual_number = self._read_cam_param(added.parameters, "tool_number")
            if actual_number is None:
                actual_number = tool_number
        except Exception:
            pass

        return {
            "success": True,
            "tool_number": actual_number,
            "description": description,
            "tool_type": tool_type,
            "type_str": type_str,
            "method_used": method_used,
            "add_result": str(add_result),
            "diameter_mm": diameter_mm,
            "document_library_count": tool_lib.count,
            "errors": errors if errors else None,
        }

    # ------------------------------------------------------------------

    def ping(self):
        return {"pong": True}

    # ------------------------------------------------------------------
    # Design type safety
    # ------------------------------------------------------------------

    def get_design_type(self):
        """Return current design type: 'parametric' or 'direct'."""
        design = self._design()
        dt = design.designType
        is_parametric = dt == adsk.fusion.DesignTypes.ParametricDesignType
        return {
            "design_type": "parametric" if is_parametric else "direct",
            "design_type_id": dt,
        }

    def set_design_type(self, design_type: str):
        """Switch design type. Use 'parametric' to recover from accidental
        direct-mode switches (equivalent to UI 'Capture Design History')."""
        design = self._design()
        current = design.designType

        if design_type == "parametric":
            target = adsk.fusion.DesignTypes.ParametricDesignType
            if current == target:
                return {
                    "changed": False,
                    "design_type": "parametric",
                    "message": "Already in parametric mode",
                }
            design.designType = target
            adsk.doEvents()
            # Verify it actually changed
            if design.designType != target:
                raise RuntimeError(
                    "Failed to switch to parametric mode. "
                    "Try 'Capture Design History' in the Fusion UI."
                )
            return {"changed": True, "design_type": "parametric"}

        elif design_type == "direct":
            target = adsk.fusion.DesignTypes.DirectDesignType
            if current == target:
                return {
                    "changed": False,
                    "design_type": "direct",
                    "message": "Already in direct mode",
                }
            design.designType = target
            adsk.doEvents()
            return {"changed": True, "design_type": "direct"}

        else:
            raise RuntimeError(
                f"Invalid design_type '{design_type}'. Use 'parametric' or 'direct'."
            )

    # ------------------------------------------------------------------
    # Code execution (REPL-style)
    # ------------------------------------------------------------------

    def execute_code(self, code: str):
        design = self._design()
        type_before = design.designType

        ns = {
            "adsk": adsk,
            "app": self.app,
            "ui": self.ui,
            "design": design,
            "component": self._root(),
            "math": math,
        }

        buf = io.StringIO()

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise RuntimeError(f"SyntaxError: {exc}")

        last_expr_value = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last_node = tree.body.pop()
            if tree.body:
                with redirect_stdout(buf):
                    exec(
                        compile(
                            ast.Module(body=tree.body, type_ignores=[]), "<mcp>", "exec"
                        ),
                        ns,
                    )
            expr_code = compile(ast.Expression(body=last_node.value), "<mcp>", "eval")
            with redirect_stdout(buf):
                last_expr_value = eval(expr_code, ns)
        else:
            with redirect_stdout(buf):
                exec(compile(tree, "<mcp>", "exec"), ns)

        output = buf.getvalue()
        result = last_expr_value if last_expr_value is not None else output

        # Warn if design type changed during execution
        type_after = design.designType
        design_type_warning = None
        if type_before != type_after:
            design_type_warning = (
                f"WARNING: Design type changed from "
                f"{'parametric' if type_before == 1 else 'direct'} to "
                f"{'parametric' if type_after == 1 else 'direct'} "
                f"during code execution. Use set_design_type to recover."
            )
            log.warning(design_type_warning)
        if result is not None:
            try:
                import json as _json

                _json.dumps(result)
            except (TypeError, ValueError):
                result = str(result)

        response = {"executed": True, "result": result, "output": output}
        if design_type_warning:
            response["design_type_warning"] = design_type_warning
        return response

    # ------------------------------------------------------------------
    # Camera helper
    # ------------------------------------------------------------------

    def _camera_info(self):
        try:
            cam = self.app.activeViewport.camera
            return {
                "eye": [cam.eye.x, cam.eye.y, cam.eye.z],
                "target": [cam.target.x, cam.target.y, cam.target.z],
                "up_vector": [cam.upVector.x, cam.upVector.y, cam.upVector.z],
            }
        except Exception:
            return None

    @staticmethod
    def _bbox_dict(bbox):
        return {
            "min": [bbox.minPoint.x, bbox.minPoint.y, bbox.minPoint.z],
            "max": [bbox.maxPoint.x, bbox.maxPoint.y, bbox.maxPoint.z],
        }

    # ------------------------------------------------------------------
    # Mutation snapshot (before/after deltas for feedback)
    # ------------------------------------------------------------------

    def _snapshot(self) -> dict | None:
        """Capture body_count, overall bbox, and total mass of the design.

        Best-effort — returns None if the design isn't readable yet.  Mass
        is reported in grams; bbox in cm (Fusion's internal unit).
        """
        try:
            design = self.app.activeProduct
            if design is None or not hasattr(design, "rootComponent"):
                return None
            root = design.rootComponent

            # Count bodies recursively (root + occurrences).
            body_count = root.bRepBodies.count
            try:
                for occ in design.rootComponent.allOccurrences:
                    body_count += occ.bRepBodies.count
            except Exception:
                pass  # allOccurrences can fail on empty designs

            bbox_dict = None
            if body_count > 0:
                try:
                    bbox = root.boundingBox
                    if bbox is not None:
                        bbox_dict = self._bbox_dict(bbox)
                except Exception:
                    bbox_dict = None

            mass_g = 0.0
            if body_count > 0:
                try:
                    # physicalProperties.mass is in kg
                    mass_g = float(root.physicalProperties.mass) * 1000.0
                except Exception:
                    mass_g = 0.0

            return {
                "body_count": body_count,
                "bbox": bbox_dict,
                "mass_g": mass_g,
            }
        except Exception as exc:
            log.debug("snapshot failed: %s", exc)
            return None

    @staticmethod
    def _compute_deltas(before: dict, after: dict) -> dict:
        """Return a diff suitable for an agent: counts + masses + bboxes."""
        return {
            "body_count_before": before.get("body_count", 0),
            "body_count_after": after.get("body_count", 0),
            "body_count_delta": after.get("body_count", 0)
            - before.get("body_count", 0),
            "mass_g_before": before.get("mass_g", 0.0),
            "mass_g_after": after.get("mass_g", 0.0),
            "mass_g_delta": after.get("mass_g", 0.0) - before.get("mass_g", 0.0),
            "bbox_before": before.get("bbox"),
            "bbox_after": after.get("bbox"),
        }

    # ------------------------------------------------------------------
    # Viewport render (perception)
    # ------------------------------------------------------------------

    def render_view(
        self,
        view: str = "current",
        width: int = 1024,
        height: int = 768,
        fit: bool = True,
    ):
        """Save the active viewport to a PNG and return base64-encoded bytes.

        * ``view`` — ``"current"`` keeps the existing camera, or one of
          ``_VIEW_DIRS`` keys (iso, front, top, ...) to reposition first.
        * ``width``/``height`` — pixel dimensions.
        * ``fit`` — call viewport.fit() before capture so the model frames.

        If ``view != "current"``, the camera is restored to its prior state
        before returning so the user's view isn't disturbed.
        """
        viewport = self.app.activeViewport
        if viewport is None:
            raise RuntimeError("No active viewport")

        repositioned = view != "current"
        if repositioned:
            spec = self._VIEW_DIRS.get(view)
            if spec is None:
                raise RuntimeError(
                    f"Unknown view '{view}'. "
                    f"Expected: current, {', '.join(self._VIEW_DIRS)}"
                )
            orig = viewport.camera
            orig_state = {
                "eye": (orig.eye.x, orig.eye.y, orig.eye.z),
                "target": (orig.target.x, orig.target.y, orig.target.z),
                "up": (orig.upVector.x, orig.upVector.y, orig.upVector.z),
                "type": orig.cameraType,
            }
            self._orient_camera(viewport, spec)

        try:
            if fit:
                try:
                    viewport.fit()
                except Exception:
                    pass  # fit() can fail on empty designs; keep going

            # saveAsImageFile requires a real path; write to a tempfile.
            fd, path = tempfile.mkstemp(suffix=".png", prefix="fusion_render_")
            os.close(fd)
            try:
                ok = viewport.saveAsImageFile(path, int(width), int(height))
                if not ok or not os.path.exists(path):
                    raise RuntimeError("saveAsImageFile returned false")
                with open(path, "rb") as f:
                    data = f.read()
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass
        finally:
            if repositioned:
                cam = viewport.camera
                cam.isSmoothTransition = False
                cam.cameraType = orig_state["type"]
                cam.eye = adsk.core.Point3D.create(*orig_state["eye"])
                cam.target = adsk.core.Point3D.create(*orig_state["target"])
                cam.upVector = adsk.core.Vector3D.create(*orig_state["up"])
                viewport.camera = cam

        return {
            "view": view,
            "width": int(width),
            "height": int(height),
            "image_format": "png",
            "image_base64": base64.b64encode(data).decode("ascii"),
            "bytes": len(data),
        }

    def _orient_camera(self, viewport, spec):
        """Position the camera at a canonical view relative to the model.

        ``spec`` is ``(eye_dir, up_vec)`` from ``_VIEW_DIRS``.
        """
        eye_dir, up_vec = spec
        design = self.app.activeProduct
        root = design.rootComponent if design is not None else None

        # Target is the model centroid (or origin if no bodies).
        target = adsk.core.Point3D.create(0.0, 0.0, 0.0)
        distance = 20.0
        if root is not None and root.bRepBodies.count > 0:
            try:
                bbox = root.boundingBox
                if bbox is not None:
                    cx = (bbox.minPoint.x + bbox.maxPoint.x) * 0.5
                    cy = (bbox.minPoint.y + bbox.maxPoint.y) * 0.5
                    cz = (bbox.minPoint.z + bbox.maxPoint.z) * 0.5
                    target = adsk.core.Point3D.create(cx, cy, cz)
                    dx = bbox.maxPoint.x - bbox.minPoint.x
                    dy = bbox.maxPoint.y - bbox.minPoint.y
                    dz = bbox.maxPoint.z - bbox.minPoint.z
                    distance = max(dx, dy, dz, 1.0) * 2.5
            except Exception:
                pass

        eye = adsk.core.Point3D.create(
            target.x + eye_dir[0] * distance,
            target.y + eye_dir[1] * distance,
            target.z + eye_dir[2] * distance,
        )
        up = adsk.core.Vector3D.create(up_vec[0], up_vec[1], up_vec[2])

        cam = viewport.camera
        cam.eye = eye
        cam.target = target
        cam.upVector = up
        cam.isSmoothTransition = False
        viewport.camera = cam
