# -*- coding: utf-8 -*-
"""
ClickAttributeEditor

- Click a feature and edit a chosen attribute through a popup input.
- SHIFT + right-click changes the target field.
- Automatically deactivates when another QGIS map tool is activated.
- No notification spam: messages appear only for warnings/errors.
- Toolbar icon/button text shows only the active field name.

QGIS 4 / Qt6 compatible version.
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QIcon, QCursor
from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox

from qgis.core import NULL, QgsVectorLayer
from qgis.gui import QgsMapToolIdentify


# ========= CONFIG =========
AUTO_START_EDITING = True   # Automatically start editing if not already enabled
AUTO_COMMIT = False         # True = commit on every edit. Can be slow.
WINDOW_TITLE = "Edit attribute (on click)"
SEARCH_RADIUS_PX = 12       # Pixel tolerance converted to map units
ICON_FILENAME = "icon.png"  # Icon file inside the plugin folder
# ==========================


def _qt_enum(group_name, enum_name, old_name):
    """
    Compatibility helper for Qt5 / Qt6 enum locations.

    Qt5 style:
        Qt.CrossCursor
        Qt.ShiftModifier
        Qt.RightButton

    Qt6 style:
        Qt.CursorShape.CrossCursor
        Qt.KeyboardModifier.ShiftModifier
        Qt.MouseButton.RightButton
    """
    group = getattr(Qt, group_name, None)

    if group is not None and hasattr(group, enum_name):
        return getattr(group, enum_name)

    return getattr(Qt, old_name)


CROSS_CURSOR = _qt_enum("CursorShape", "CrossCursor", "CrossCursor")
SHIFT_MODIFIER = _qt_enum("KeyboardModifier", "ShiftModifier", "ShiftModifier")
RIGHT_BUTTON = _qt_enum("MouseButton", "RightButton", "RightButton")


def _is_numeric_field(field) -> bool:
    """Robust numeric check using QgsField.isNumeric()."""
    try:
        return field.isNumeric()
    except Exception:
        return False


def _to_float_or_default(value, default=0.0) -> float:
    """Convert a QGIS/Python value to float, falling back to a default."""
    if value in (None, NULL, ""):
        return float(default)

    try:
        return float(value)
    except Exception:
        return float(default)


def _is_vector_layer(layer) -> bool:
    """Return True when the active layer is a valid QgsVectorLayer."""
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

    QGIS map tools receive QgsMapMouseEvent. In QGIS 4, event.x()/event.y()
    may not be exposed, so pixelPoint() is preferred.
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

    # Last fallback for older APIs
    return int(event.x()), int(event.y())


def _event_is_shift_right_click(event) -> bool:
    """Return True when the event is SHIFT + right-click."""
    try:
        is_shift = bool(event.modifiers() & SHIFT_MODIFIER)
    except Exception:
        is_shift = False

    try:
        is_right_click = event.button() == RIGHT_BUTTON
    except Exception:
        is_right_click = False

    return is_shift and is_right_click


def _get_first_identified_feature(result):
    """
    Extract QgsFeature from a QgsMapToolIdentify.IdentifyResult.

    Usually the feature is stored in mFeature. This helper keeps the code
    resilient if a future binding exposes it differently.
    """
    if hasattr(result, "mFeature"):
        return result.mFeature

    if hasattr(result, "feature"):
        feature_attr = result.feature
        if callable(feature_attr):
            return feature_attr()
        return feature_attr

    return None


class ClickEditTool(QgsMapToolIdentify):
    """
    Map tool that identifies a feature under the cursor and edits one attribute.

    Uses QgsMapToolIdentify to allow pixel tolerance, behaving closer to
    QGIS native selection/identify tools.
    """

    def __init__(self, iface, plugin):
        super().__init__(iface.mapCanvas())

        self.iface = iface
        self.plugin = plugin

        self.setCursor(QCursor(CROSS_CURSOR))

    def _identify_hits(self, event, layer):
        """
        Identify features on the active layer using a QGIS 4-safe method.

        The documented identify() signature accepts:
            identify(x, y, layerList, mode, identifyContext)

        So SEARCH_RADIUS_PX is not passed as a positional argument. Instead,
        it is converted to map units and applied through identify properties.
        """
        x, y = _event_pixel_xy(event)

        canvas = self.iface.mapCanvas()
        search_radius_map_units = SEARCH_RADIUS_PX * canvas.mapUnitsPerPixel()

        properties_applied = False

        try:
            # Preferred for newer QGIS versions
            if hasattr(QgsMapToolIdentify, "IdentifyProperties") and hasattr(
                self, "setPropertiesOverrides"
            ):
                properties = QgsMapToolIdentify.IdentifyProperties()
                properties.searchRadiusMapUnits = search_radius_map_units
                self.setPropertiesOverrides(properties)
                properties_applied = True

            # Older fallback, deprecated in recent QGIS 3 but still useful
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
                # Alternative signature used by some QGIS bindings:
                # identify(x, y, mode, layerList, layerType)
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

    def canvasReleaseEvent(self, event):
        layer = self.iface.activeLayer()

        if layer is None or not layer.isValid():
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                "Please activate a valid vector layer.",
            )
            return

        if not _is_vector_layer(layer):
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                "The active layer must be a vector layer.",
            )
            return

        # SHIFT + right-click = quickly change target field
        if _event_is_shift_right_click(event) or not self.plugin.target_field:
            if not self.plugin.choose_field(layer):
                return  # User cancelled

        idx = layer.fields().indexFromName(self.plugin.target_field)

        if idx < 0:
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                f"Field '{self.plugin.target_field}' does not exist in the active layer.",
            )
            return

        # Identify feature with pixel tolerance
        try:
            hits = self._identify_hits(event, layer)
        except Exception as exc:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ClickAttributeEditor",
                f"Feature identification failed:\n\n{exc}",
            )
            return

        if not hits:
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                f"No feature found under cursor. Try increasing SEARCH_RADIUS_PX "
                f"(current: {SEARCH_RADIUS_PX}).",
            )
            return

        feature = _get_first_identified_feature(hits[0])

        if feature is None or not feature.isValid():
            self.iface.messageBar().pushWarning(
                "ClickAttributeEditor",
                "A feature was identified, but it could not be read.",
            )
            return

        # Ensure editing mode
        if not layer.isEditable():
            if AUTO_START_EDITING:
                if not layer.startEditing():
                    self.iface.messageBar().pushWarning(
                        "ClickAttributeEditor",
                        "Could not start editing on the active layer.",
                    )
                    return
            else:
                self.iface.messageBar().pushWarning(
                    "ClickAttributeEditor",
                    "The layer is not in edit mode.",
                )
                return

        field_def = layer.fields()[idx]
        current_value = feature[self.plugin.target_field]

        # Input dialog
        if _is_numeric_field(field_def):
            start_value = _to_float_or_default(current_value, default=0.0)

            new_value, ok = QInputDialog.getDouble(
                self.iface.mainWindow(),
                WINDOW_TITLE,
                f"{self.plugin.target_field} (FID {feature.id()})",
                start_value,
                -1e18,
                1e18,
                6,
            )

            if not ok:
                return

        else:
            new_value, ok = QInputDialog.getText(
                self.iface.mainWindow(),
                WINDOW_TITLE,
                f"{self.plugin.target_field} (FID {feature.id()})",
                text="" if current_value in (None, NULL) else str(current_value),
            )

            if not ok:
                return

        # Apply change
        if not layer.changeAttributeValue(feature.id(), idx, new_value):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ClickAttributeEditor",
                "Failed to update attribute value.",
            )
            return

        layer.triggerRepaint()

        if AUTO_COMMIT:
            if not layer.commitChanges():
                layer.rollBack()

                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "ClickAttributeEditor",
                    "Commit failed. Changes were rolled back.",
                )
                return

            layer.startEditing()

        # No success message intentionally


class ClickAttributeEditor:
    def __init__(self, iface):
        self.iface = iface

        self.action = None
        self.action_choose = None

        self.tool = None
        self.prev_tool = None
        self.target_field = None

        self.icon = QIcon()

    # --------- auto-disable when switching tools ----------

    def _on_map_tool_set(self, new_tool, old_tool=None):
        """
        Auto-uncheck this plugin if another map tool is selected.

        Signal signatures can vary slightly, so old_tool has a default.
        """
        if self.action and self.action.isChecked() and new_tool is not self.tool:
            self.action.blockSignals(True)
            self.action.setChecked(False)
            self.action.blockSignals(False)

            self.prev_tool = None

            self.iface.messageBar().pushInfo(
                "ClickAttributeEditor",
                "Tool deactivated because another map tool was selected.",
            )

    def initGui(self):
        # Load icon from plugin folder
        icon_path = os.path.join(os.path.dirname(__file__), ICON_FILENAME)

        if os.path.exists(icon_path):
            self.icon = QIcon(icon_path)

        # Main toolbar action
        # Button text shows only the active field name
        self.action = QAction(self.icon, "", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip(
            "ClickAttributeEditor: click a feature to edit a field "
            "(SHIFT + right-click to change the target field)."
        )
        self.action.triggered.connect(self.toggle_tool)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("ClickAttributeEditor", self.action)

        # Secondary menu action: change field without SHIFT
        self.action_choose = QAction(
            self.icon,
            "Set target field…",
            self.iface.mainWindow(),
        )
        self.action_choose.triggered.connect(self.choose_field_from_active_layer)

        self.iface.addPluginToMenu("ClickAttributeEditor", self.action_choose)

        self.tool = ClickEditTool(self.iface, self)

        # Listen for tool changes to auto-disable
        try:
            self.iface.mapCanvas().mapToolSet.connect(self._on_map_tool_set)
        except Exception:
            pass

        # Optionally ask for field once if a vector layer is active
        layer = self.iface.activeLayer()

        if _is_vector_layer(layer):
            self.choose_field(layer)

    def unload(self):
        try:
            self.iface.mapCanvas().mapToolSet.disconnect(self._on_map_tool_set)
        except Exception:
            pass

        if self.action:
            try:
                self.iface.removePluginMenu("ClickAttributeEditor", self.action)
            except Exception:
                pass

            try:
                self.iface.removeToolBarIcon(self.action)
            except Exception:
                pass

        if self.action_choose:
            try:
                self.iface.removePluginMenu("ClickAttributeEditor", self.action_choose)
            except Exception:
                pass

    def toggle_tool(self, checked):
        canvas = self.iface.mapCanvas()

        if checked:
            self.prev_tool = canvas.mapTool()
            canvas.setMapTool(self.tool)

        else:
            if self.prev_tool:
                canvas.setMapTool(self.prev_tool)
                self.prev_tool = None

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
            "Which field should be edited on click? "
            "(SHIFT + right-click changes it quickly)",
            fields,
            fields.index(current),
            False,
        )

        if not ok or not choice:
            return False

        self.target_field = choice

        # Toolbar button text = field name only
        if self.action:
            self.action.setText(self.target_field)

        return True
