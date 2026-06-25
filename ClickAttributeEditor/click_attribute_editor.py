# -*- coding: utf-8 -*-
"""
ClickAttributeEditor

- Quick click: edit one clicked feature.
- Click + hold + drag: freehand bulk update for all intersecting features.
- SHIFT + right-click changes the target field.
- Automatically deactivates when another QGIS map tool is activated.
- Silent when no feature is clicked.
- QGIS 4 / Qt6 compatible version.
"""

import os
import math

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QIcon, QCursor
from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox

from qgis.core import (
    NULL,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
    QgsWkbTypes,
)

from qgis.gui import QgsMapToolIdentify, QgsRubberBand


# ========= CONFIG =========
AUTO_START_EDITING = True
AUTO_COMMIT = False

WINDOW_TITLE = "Edit attribute"
SEARCH_RADIUS_PX = 12
DRAG_THRESHOLD_PX = 6

ICON_FILENAME = "icon.png"
BULK_MIN_POINTS = 3
# ==========================


def _qt_enum(group_name, enum_name, old_name):
    """
    Compatibility helper for Qt5 / Qt6 enum locations.
    """
    group = getattr(Qt, group_name, None)

    if group is not None and hasattr(group, enum_name):
        return getattr(group, enum_name)

    return getattr(Qt, old_name)


CROSS_CURSOR = _qt_enum("CursorShape", "CrossCursor", "CrossCursor")
SHIFT_MODIFIER = _qt_enum("KeyboardModifier", "ShiftModifier", "ShiftModifier")
LEFT_BUTTON = _qt_enum("MouseButton", "LeftButton", "LeftButton")
RIGHT_BUTTON = _qt_enum("MouseButton", "RightButton", "RightButton")


def _polygon_geometry_type():
    """
    Compatibility helper for QgsWkbTypes polygon geometry enum.
    """
    geometry_type = getattr(QgsWkbTypes, "GeometryType", None)

    if geometry_type is not None and hasattr(geometry_type, "PolygonGeometry"):
        return geometry_type.PolygonGeometry

    return QgsWkbTypes.PolygonGeometry


POLYGON_GEOMETRY = _polygon_geometry_type()


def _is_numeric_field(field) -> bool:
    try:
        return field.isNumeric()
    except Exception:
        return False


def _to_float_or_default(value, default=0.0) -> float:
    if value in (None, NULL, ""):
        return float(default)

    try:
        return float(value)
    except Exception:
        return float(default)


def _is_vector_layer(layer) -> bool:
    try:
        return (
            layer is not None
            and layer.isValid()
            and isinstance(layer, QgsVectorLayer)
        )
    except Exception:
        return False


def _event_pixel_xy(event):
    """
    Return mouse event pixel coordinates in a QGIS 3 / QGIS 4 safe way.
    """
    if hasattr(event, "pixelPoint"):
        point = event.pixelPoint()
        return int(point.x()), int(point.y())

    if hasattr(event, "originalPixelPoint"):
        point = event.originalPixelPoint()
        return int(point.x()), int(point.y())

    if hasattr(event, "pos"):
        point = event.pos()
        return int(point.x()), int(point.y())

    return int(event.x()), int(event.y())


def _event_map_point(event, canvas):
    """
    Return mouse event map coordinates as QgsPointXY.
    """
    if hasattr(event, "mapPoint"):
        point = event.mapPoint()
        return QgsPointXY(point)

    if hasattr(event, "originalMapPoint"):
        point = event.originalMapPoint()
        return QgsPointXY(point)

    x, y = _event_pixel_xy(event)
    point = canvas.getCoordinateTransform().toMapCoordinates(x, y)
    return QgsPointXY(point)


def _event_is_shift_right_click(event) -> bool:
    try:
        is_shift = bool(event.modifiers() & SHIFT_MODIFIER)
    except Exception:
        is_shift = False

    try:
        is_right_click = event.button() == RIGHT_BUTTON
    except Exception:
        is_right_click = False

    return is_shift and is_right_click


def _event_is_left_button(event) -> bool:
    try:
        return event.button() == LEFT_BUTTON
    except Exception:
        return False


def _get_first_identified_feature(result):
    """
    Extract QgsFeature from QgsMapToolIdentify.IdentifyResult.
    """
    if hasattr(result, "mFeature"):
        return result.mFeature

    if hasattr(result, "feature"):
        feature_attr = result.feature

        if callable(feature_attr):
            return feature_attr()

        return feature_attr

    return None


def _feature_geometry(feature):
    try:
        geometry = feature.geometry()

        if geometry is None or geometry.isEmpty():
            return None

        return geometry

    except Exception:
        return None


class ClickEditTool(QgsMapToolIdentify):
    """
    Single map tool:

    - quick click = point update
    - click + drag = freehand bulk update
    """

    def __init__(self, iface, plugin):
        super().__init__(iface.mapCanvas())

        self.iface = iface
        self.plugin = plugin

        self.left_press_active = False
        self.drag_started = False

        self.press_pixel = None
        self.press_map_point = None

        self.bulk_points = []
        self.rubber_band = None

        self.setCursor(QCursor(CROSS_CURSOR))

    # --------------------------
    # General helpers
    # --------------------------

    def _active_vector_layer_or_warn(self):
        layer = self.iface.activeLayer()

        if layer is None or not layer.isValid():
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                "Please activate a valid vector layer.",
            )
            return None

        if not _is_vector_layer(layer):
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                "The active layer must be a vector layer.",
            )
            return None

        return layer

    def _ensure_editing(self, layer) -> bool:
        if layer.isEditable():
            return True

        if AUTO_START_EDITING:
            if layer.startEditing():
                return True

            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                "Could not start editing on the active layer.",
            )
            return False

        self.iface.messageBar().pushWarning(
            "ClickAttributeEditor",
            "The layer is not in edit mode.",
        )
        return False

    def _target_field_index_or_warn(self, layer):
        if not self.plugin.target_field:
            if not self.plugin.choose_field(layer):
                return -1

        idx = layer.fields().indexFromName(self.plugin.target_field)

        if idx < 0:
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                f"Field '{self.plugin.target_field}' does not exist in the active layer.",
            )
            return -1

        return idx

    def _ask_new_value(self, layer, idx, current_value=None, label_suffix=""):
        field_def = layer.fields()[idx]

        label = self.plugin.target_field

        if label_suffix:
            label = f"{label} {label_suffix}"

        if _is_numeric_field(field_def):
            start_value = _to_float_or_default(current_value, default=0.0)

            new_value, ok = QInputDialog.getDouble(
                self.iface.mainWindow(),
                WINDOW_TITLE,
                label,
                start_value,
                -1e18,
                1e18,
                6,
            )

            if not ok:
                return None, False

            return new_value, True

        new_value, ok = QInputDialog.getText(
            self.iface.mainWindow(),
            WINDOW_TITLE,
            label,
            text="" if current_value in (None, NULL) else str(current_value),
        )

        if not ok:
            return None, False

        return new_value, True

    def _apply_value_to_features(self, layer, idx, feature_ids, new_value):
        if not feature_ids:
            return 0, 0

        updated = 0
        failed = 0

        try:
            layer.beginEditCommand("ClickAttributeEditor update")
        except Exception:
            pass

        for fid in feature_ids:
            if layer.changeAttributeValue(fid, idx, new_value):
                updated += 1
            else:
                failed += 1

        try:
            layer.endEditCommand()
        except Exception:
            pass

        layer.triggerRepaint()

        if AUTO_COMMIT:
            if not layer.commitChanges():
                layer.rollBack()

                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "ClickAttributeEditor",
                    "Commit failed. Changes were rolled back.",
                )

                return updated, failed

            layer.startEditing()

        return updated, failed

    def _reset_interaction(self):
        self.left_press_active = False
        self.drag_started = False

        self.press_pixel = None
        self.press_map_point = None

        self.bulk_points = []

        if self.rubber_band is not None:
            try:
                self.rubber_band.reset(POLYGON_GEOMETRY)
            except Exception:
                pass

    # --------------------------
    # Point update
    # --------------------------

    def _identify_hits(self, event, layer):
        x, y = _event_pixel_xy(event)

        canvas = self.iface.mapCanvas()
        search_radius_map_units = SEARCH_RADIUS_PX * canvas.mapUnitsPerPixel()

        properties_applied = False

        try:
            if hasattr(QgsMapToolIdentify, "IdentifyProperties") and hasattr(
                self, "setPropertiesOverrides"
            ):
                properties = QgsMapToolIdentify.IdentifyProperties()
                properties.searchRadiusMapUnits = search_radius_map_units
                self.setPropertiesOverrides(properties)
                properties_applied = True

            elif hasattr(self, "setCanvasPropertiesOverrides"):
                self.setCanvasPropertiesOverrides(search_radius_map_units)
                properties_applied = True

            try:
                return self.identify(
                    x,
                    y,
                    [layer],
                    QgsMapToolIdentify.TopDownStopAtFirst,
                )

            except TypeError:
                return self.identify(
                    x,
                    y,
                    QgsMapToolIdentify.TopDownStopAtFirst,
                    [layer],
                    QgsMapToolIdentify.VectorLayer,
                )

        finally:
            if properties_applied:
                try:
                    if hasattr(self, "restorePropertiesOverrides"):
                        self.restorePropertiesOverrides()
                    elif hasattr(self, "restoreCanvasPropertiesOverrides"):
                        self.restoreCanvasPropertiesOverrides()
                except Exception:
                    pass

    def _handle_point_update(self, event):
        layer = self._active_vector_layer_or_warn()

        if layer is None:
            return

        idx = self._target_field_index_or_warn(layer)

        if idx < 0:
            return

        try:
            hits = self._identify_hits(event, layer)
        except Exception as exc:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ClickAttributeEditor",
                f"Feature identification failed:\n\n{exc}",
            )
            return

        # Requested behavior:
        # if no feature was clicked, do nothing silently.
        if not hits:
            return

        feature = _get_first_identified_feature(hits[0])

        if feature is None or not feature.isValid():
            return

        if not self._ensure_editing(layer):
            return

        current_value = feature[self.plugin.target_field]

        new_value, ok = self._ask_new_value(
            layer,
            idx,
            current_value=current_value,
            label_suffix=f"(FID {feature.id()})",
        )

        if not ok:
            return

        updated, failed = self._apply_value_to_features(
            layer,
            idx,
            [feature.id()],
            new_value,
        )

        if failed:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ClickAttributeEditor",
                "Failed to update attribute value.",
            )

    # --------------------------
    # Freehand bulk update
    # --------------------------

    def _ensure_rubber_band(self):
        if self.rubber_band is not None:
            return

        self.rubber_band = QgsRubberBand(
            self.iface.mapCanvas(),
            POLYGON_GEOMETRY,
        )

        try:
            self.rubber_band.setWidth(2)
        except Exception:
            pass

    def _update_rubber_band(self):
        if len(self.bulk_points) < BULK_MIN_POINTS:
            return

        self._ensure_rubber_band()

        ring = list(self.bulk_points)

        if ring[0] != ring[-1]:
            ring.append(ring[0])

        geometry = QgsGeometry.fromPolygonXY([ring])

        try:
            self.rubber_band.setToGeometry(geometry, None)
            self.rubber_band.show()
        except Exception:
            pass

    def _bulk_selection_geometry(self):
        if len(self.bulk_points) < BULK_MIN_POINTS:
            return None

        ring = list(self.bulk_points)

        if ring[0] != ring[-1]:
            ring.append(ring[0])

        geometry = QgsGeometry.fromPolygonXY([ring])

        if geometry is None or geometry.isEmpty():
            return None

        return geometry

    def _feature_ids_intersecting_geometry(self, layer, selection_geometry):
        feature_ids = []

        request = QgsFeatureRequest()
        request.setFilterRect(selection_geometry.boundingBox())

        for feature in layer.getFeatures(request):
            geometry = _feature_geometry(feature)

            if geometry is None:
                continue

            try:
                if selection_geometry.intersects(geometry):
                    feature_ids.append(feature.id())
            except Exception:
                continue

        return feature_ids

    def _handle_bulk_update(self):
        layer = self._active_vector_layer_or_warn()

        if layer is None:
            return

        selection_geometry = self._bulk_selection_geometry()

        if selection_geometry is None:
            return

        idx = self._target_field_index_or_warn(layer)

        if idx < 0:
            return

        feature_ids = self._feature_ids_intersecting_geometry(
            layer,
            selection_geometry,
        )

        # If the drawn area finds no features, do nothing silently.
        if not feature_ids:
            return

        if not self._ensure_editing(layer):
            return

        new_value, ok = self._ask_new_value(
            layer,
            idx,
            current_value=None,
            label_suffix=f"({len(feature_ids)} selected features)",
        )

        if not ok:
            return

        updated, failed = self._apply_value_to_features(
            layer,
            idx,
            feature_ids,
            new_value,
        )

        if failed:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ClickAttributeEditor",
                f"Bulk update completed with errors.\n\n"
                f"Updated: {updated}\n"
                f"Failed: {failed}",
            )
        else:
            self.iface.messageBar().pushInfo(
                "ClickAttributeEditor",
                f"Bulk update completed: {updated} feature(s) updated.",
            )

    # --------------------------
    # QGIS map tool events
    # --------------------------

    def canvasPressEvent(self, event):
        if _event_is_shift_right_click(event):
            layer = self._active_vector_layer_or_warn()

            if layer is not None:
                self.plugin.choose_field(layer)

            self._reset_interaction()
            return

        if not _event_is_left_button(event):
            self._reset_interaction()
            return

        canvas = self.iface.mapCanvas()

        self.left_press_active = True
        self.drag_started = False

        self.press_pixel = _event_pixel_xy(event)
        self.press_map_point = _event_map_point(event, canvas)

        self.bulk_points = [self.press_map_point]

    def canvasMoveEvent(self, event):
        if not self.left_press_active:
            return

        canvas = self.iface.mapCanvas()

        current_pixel = _event_pixel_xy(event)
        current_map_point = _event_map_point(event, canvas)

        if self.press_pixel is None:
            return

        dx = current_pixel[0] - self.press_pixel[0]
        dy = current_pixel[1] - self.press_pixel[1]

        distance_px = math.sqrt(dx * dx + dy * dy)

        if not self.drag_started:
            if distance_px < DRAG_THRESHOLD_PX:
                return

            self.drag_started = True
            self._ensure_rubber_band()

        if self.bulk_points:
            last_point = self.bulk_points[-1]

            if last_point == current_map_point:
                return

        self.bulk_points.append(current_map_point)
        self._update_rubber_band()

    def canvasReleaseEvent(self, event):
        if _event_is_shift_right_click(event):
            layer = self._active_vector_layer_or_warn()

            if layer is not None:
                self.plugin.choose_field(layer)

            self._reset_interaction()
            return

        if not _event_is_left_button(event):
            self._reset_interaction()
            return

        if not self.left_press_active:
            self._reset_interaction()
            return

        canvas = self.iface.mapCanvas()
        release_map_point = _event_map_point(event, canvas)

        if self.drag_started:
            self.bulk_points.append(release_map_point)
            self._update_rubber_band()

            try:
                self._handle_bulk_update()
            finally:
                self._reset_interaction()

            return

        try:
            self._handle_point_update(event)
        finally:
            self._reset_interaction()

    def deactivate(self):
        self._reset_interaction()
        super().deactivate()


class ClickAttributeEditor:
    def __init__(self, iface):
        self.iface = iface

        self.action = None
        self.action_choose = None

        self.tool = None
        self.prev_tool = None
        self.target_field = None

        self.icon = QIcon()

    # --------------------------
    # GUI helpers
    # --------------------------

    def _set_action_checked_safely(self, checked):
        if self.action is None:
            return

        self.action.blockSignals(True)
        self.action.setChecked(checked)
        self.action.blockSignals(False)

    def _update_action_text(self):
        field_text = self.target_field if self.target_field else "No field"

        if self.action:
            self.action.setText(f"Edit: {field_text}")

    def _on_map_tool_set(self, new_tool, old_tool=None):
        """
        Auto-uncheck plugin action if another map tool is selected.
        """
        if new_tool is self.tool:
            return

        if self.action and self.action.isChecked():
            self._set_action_checked_safely(False)
            self.prev_tool = None

            self.iface.messageBar().pushInfo(
                "ClickAttributeEditor",
                "Tool deactivated because another map tool was selected.",
            )

    # --------------------------
    # QGIS plugin lifecycle
    # --------------------------

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), ICON_FILENAME)

        if os.path.exists(icon_path):
            self.icon = QIcon(icon_path)

        self.tool = ClickEditTool(self.iface, self)

        self.action = QAction(
            self.icon,
            "Edit: No field",
            self.iface.mainWindow(),
        )
        self.action.setCheckable(True)
        self.action.setToolTip(
            "ClickAttributeEditor: click a feature to edit it. "
            "Click, hold and drag to bulk update features. "
            "SHIFT + right-click changes the target field."
        )
        self.action.triggered.connect(self.toggle_tool)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("ClickAttributeEditor", self.action)

        self.action_choose = QAction(
            self.icon,
            "Set target field…",
            self.iface.mainWindow(),
        )
        self.action_choose.triggered.connect(self.choose_field_from_active_layer)

        self.iface.addPluginToMenu("ClickAttributeEditor", self.action_choose)

        try:
            self.iface.mapCanvas().mapToolSet.connect(self._on_map_tool_set)
        except Exception:
            pass

        layer = self.iface.activeLayer()

        if _is_vector_layer(layer):
            self.choose_field(layer)

        self._update_action_text()

    def unload(self):
        try:
            self.iface.mapCanvas().mapToolSet.disconnect(self._on_map_tool_set)
        except Exception:
            pass

        for action in (self.action, self.action_choose):
            if action:
                try:
                    self.iface.removePluginMenu("ClickAttributeEditor", action)
                except Exception:
                    pass

        if self.action:
            try:
                self.iface.removeToolBarIcon(self.action)
            except Exception:
                pass

    # --------------------------
    # Tool activation
    # --------------------------

    def toggle_tool(self, checked):
        canvas = self.iface.mapCanvas()

        if checked:
            self.prev_tool = canvas.mapTool()
            canvas.setMapTool(self.tool)

        else:
            if self.prev_tool:
                canvas.setMapTool(self.prev_tool)
                self.prev_tool = None

    # --------------------------
    # Field selection
    # --------------------------

    def choose_field_from_active_layer(self):
        layer = self.iface.activeLayer()

        if not _is_vector_layer(layer):
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                "Please activate a vector layer first.",
            )
            return

        self.choose_field(layer)

    def choose_field(self, layer) -> bool:
        fields = [field.name() for field in layer.fields()]

        if not fields:
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                "The active layer has no fields.",
            )
            return False

        current = self.target_field if self.target_field in fields else fields[0]

        choice, ok = QInputDialog.getItem(
            self.iface.mainWindow(),
            "Choose field",
            "Which field should be edited?",
            fields,
            fields.index(current),
            False,
        )

        if not ok or not choice:
            return False

        self.target_field = choice
        self._update_action_text()

        return True
