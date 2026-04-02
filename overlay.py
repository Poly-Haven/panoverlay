from __future__ import annotations

import ctypes
import math
from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from data import ImageNode, OverlayModel


GWL_EXSTYLE = -20
SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
HANDLE_SIZE = 12.0
MIN_BOX_WIDTH = 320.0
MIN_BOX_HEIGHT = 180.0
LINE_ENDPOINT_PADDING = 12.0


class OverlayWindow(QWidget):
    boxChanged = pyqtSignal(QRect)

    def __init__(self):
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.model: OverlayModel | None = None
        self.relative_distances = False
        self.edit_mode = False
        self.drag_mode: str | None = None
        self.drag_handle: str | None = None
        self.drag_start_pos = QPointF()
        self.drag_start_rect = QRectF()
        self.box_rect = QRectF()
        self._set_fullscreen_geometry()
        self.show()

    def _set_fullscreen_geometry(self):
        geometry = virtual_desktop_geometry()
        self.setGeometry(geometry)
        if self.box_rect.isEmpty():
            self.box_rect = default_box_rect(QRectF(self.rect()), geometry)
        else:
            self.box_rect = clamp_rect_to_bounds(self.box_rect, QRectF(self.rect()))

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_click_through()

    def set_model(self, model: OverlayModel | None):
        self.model = model
        self.update()

    def set_relative_distances(self, enabled: bool):
        self.relative_distances = enabled
        self.update()

    def set_box_rect(self, rect: QRectF | QRect):
        self.box_rect = clamp_rect_to_bounds(QRectF(rect), QRectF(self.rect()))
        self.boxChanged.emit(self.box_rect.toRect())
        self.update()

    def get_box_rect(self) -> QRect:
        return self.box_rect.toRect()

    def set_edit_mode(self, enabled: bool):
        self.edit_mode = enabled
        self.drag_mode = None
        self.drag_handle = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not enabled)
        self._apply_click_through()
        self.update()

    def toggle_edit_mode(self) -> bool:
        self.set_edit_mode(not self.edit_mode)
        return self.edit_mode

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.box_rect = clamp_rect_to_bounds(self.box_rect, QRectF(self.rect()))

    def _apply_click_through(self):
        hwnd = int(self.winId())
        if not hwnd:
            return

        user32 = ctypes.windll.user32
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        exstyle |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
        if self.edit_mode:
            exstyle &= ~WS_EX_TRANSPARENT
        else:
            exstyle |= WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)
        user32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER,
        )

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        painter.fillRect(self.rect(), QColor(0, 0, 0, 0))
        preview_rect = self.preview_rect()
        self._draw_relationships(painter, preview_rect)
        self._draw_nodes(painter, preview_rect)
        self._draw_box(painter, preview_rect)

    def preview_rect(self) -> QRectF:
        box = QRectF(self.box_rect)
        if box.width() <= 0 or box.height() <= 0:
            return QRectF()

        target_aspect = 2.0
        box_aspect = box.width() / box.height()
        if box_aspect >= target_aspect:
            width = box.height() * target_aspect
            left = box.left() + (box.width() - width) / 2
            return QRectF(left, box.top(), width, box.height())

        height = box.width() / target_aspect
        top = box.top() + (box.height() - height) / 2
        return QRectF(box.left(), top, box.width(), height)

    def _draw_relationships(self, painter: QPainter, preview_rect: QRectF):
        if not self.model or preview_rect.isEmpty():
            return

        nodes = {node.image_id: node for node in self.model.images}
        count_range = relationship_count_percentile_range(self.model)
        distance_range = relationship_distance_percentile_range(self.model)
        for relationship in self.model.pairs:
            first = nodes.get(relationship.image_1)
            second = nodes.get(relationship.image_2)
            if first is None or second is None:
                continue

            start = map_node_to_rect(first, preview_rect)
            end = map_node_to_rect(second, preview_rect)
            if self.relative_distances:
                color = color_for_relative_distance(relationship.average_distance, distance_range)
                width = width_for_relative_count(relationship.count, count_range)
            else:
                color = color_for_distance(relationship.average_distance)
                width = width_for_count(relationship.count)

            pen = QPen(color)
            pen.setWidthF(width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            for segment_start, segment_end in wrapped_line_segments(start, end, preview_rect):
                trimmed = trim_line_segment(segment_start, segment_end, LINE_ENDPOINT_PADDING)
                if trimmed is not None:
                    painter.drawLine(trimmed[0], trimmed[1])

    def _draw_nodes(self, painter: QPainter, preview_rect: QRectF):
        if not self.model or preview_rect.isEmpty():
            return

        for node in self.model.images:
            point = map_node_to_rect(node, preview_rect)
            if self.edit_mode:
                painter.setPen(QColor(255, 255, 255, 210))
                painter.drawText(point + QPointF(6, -6), str(node.image_id))

    def _draw_box(self, painter: QPainter, preview_rect: QRectF):
        border_pen = QPen(QColor(80, 170, 255, 220) if self.edit_mode else QColor(255, 255, 255, 140))
        border_pen.setWidth(2)
        border_pen.setStyle(Qt.PenStyle.DashLine if self.edit_mode else Qt.PenStyle.SolidLine)
        painter.setPen(border_pen)
        painter.setBrush(QColor(30, 30, 30, 28 if self.edit_mode else 12))
        painter.drawRoundedRect(self.box_rect, 8, 8)

        preview_pen = QPen(QColor(255, 255, 255, 150))
        preview_pen.setWidth(1)
        painter.setPen(preview_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(preview_rect)

        if not self.edit_mode:
            return

        painter.setBrush(QColor(80, 170, 255, 220))
        painter.setPen(Qt.PenStyle.NoPen)
        for rect in self.handle_rects().values():
            painter.drawRect(rect)

    def mousePressEvent(self, event):
        if not self.edit_mode or event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)

        position = event.position()
        handle = self.handle_at(position)
        if handle:
            self.drag_mode = "resize"
            self.drag_handle = handle
        elif self.box_rect.contains(position):
            self.drag_mode = "move"
            self.drag_handle = None
        else:
            self.drag_mode = None
            self.drag_handle = None
            return

        self.drag_start_pos = position
        self.drag_start_rect = QRectF(self.box_rect)
        event.accept()

    def mouseMoveEvent(self, event):
        if not self.edit_mode:
            return super().mouseMoveEvent(event)

        position = event.position()
        if self.drag_mode is None:
            self._update_cursor(position)
            return

        delta = position - self.drag_start_pos
        if self.drag_mode == "move":
            rect = self.drag_start_rect.translated(delta)
        else:
            rect = resized_rect(self.drag_start_rect, self.drag_handle or "", delta)

        self.box_rect = clamp_rect_to_bounds(rect, QRectF(self.rect()))
        self.boxChanged.emit(self.box_rect.toRect())
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.edit_mode and event.button() == Qt.MouseButton.LeftButton:
            self.drag_mode = None
            self.drag_handle = None
            self._update_cursor(event.position())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _update_cursor(self, position: QPointF):
        handle = self.handle_at(position)
        if handle in {"top_left", "bottom_right"}:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif handle in {"top_right", "bottom_left"}:
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif handle in {"left", "right"}:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif handle in {"top", "bottom"}:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif self.box_rect.contains(position):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def handle_rects(self) -> dict[str, QRectF]:
        box = self.box_rect
        cx = box.center().x()
        cy = box.center().y()
        half = HANDLE_SIZE / 2
        return {
            "top_left": QRectF(box.left() - half, box.top() - half, HANDLE_SIZE, HANDLE_SIZE),
            "top": QRectF(cx - half, box.top() - half, HANDLE_SIZE, HANDLE_SIZE),
            "top_right": QRectF(box.right() - half, box.top() - half, HANDLE_SIZE, HANDLE_SIZE),
            "right": QRectF(box.right() - half, cy - half, HANDLE_SIZE, HANDLE_SIZE),
            "bottom_right": QRectF(box.right() - half, box.bottom() - half, HANDLE_SIZE, HANDLE_SIZE),
            "bottom": QRectF(cx - half, box.bottom() - half, HANDLE_SIZE, HANDLE_SIZE),
            "bottom_left": QRectF(box.left() - half, box.bottom() - half, HANDLE_SIZE, HANDLE_SIZE),
            "left": QRectF(box.left() - half, cy - half, HANDLE_SIZE, HANDLE_SIZE),
        }

    def handle_at(self, position: QPointF) -> str | None:
        for name, rect in self.handle_rects().items():
            if rect.contains(position):
                return name
        return None


def virtual_desktop_geometry() -> QRect:
    screens = QGuiApplication.screens()
    if not screens:
        return QRect(0, 0, 1920, 1080)

    geometry = screens[0].geometry()
    for screen in screens[1:]:
        geometry = geometry.united(screen.geometry())
    return geometry


def default_box_rect(bounds: QRectF, virtual_geometry: QRect) -> QRectF:
    primary_screen = QGuiApplication.primaryScreen()
    target = primary_screen.availableGeometry() if primary_screen else virtual_geometry
    width = min(bounds.width() * 0.72, target.width() * 0.82)
    height = width / 2.0
    max_height = min(bounds.height() * 0.72, target.height() * 0.72)
    if height > max_height:
        height = max_height
        width = height * 2.0

    width = max(width, MIN_BOX_WIDTH)
    height = max(height, MIN_BOX_HEIGHT)

    local_left = target.left() - virtual_geometry.left()
    local_top = target.top() - virtual_geometry.top()
    left = local_left + (target.width() - width) / 2.0
    top = local_top + (target.height() - height) / 2.0
    return clamp_rect_to_bounds(QRectF(left, top, width, height), bounds)


def clamp_rect_to_bounds(rect: QRectF, bounds: QRectF) -> QRectF:
    rect = QRectF(rect)
    rect = rect.normalized()
    rect.setWidth(max(rect.width(), MIN_BOX_WIDTH))
    rect.setHeight(max(rect.height(), MIN_BOX_HEIGHT))
    if rect.width() > bounds.width():
        rect.setWidth(bounds.width())
    if rect.height() > bounds.height():
        rect.setHeight(bounds.height())

    if rect.left() < bounds.left():
        rect.moveLeft(bounds.left())
    if rect.top() < bounds.top():
        rect.moveTop(bounds.top())
    if rect.right() > bounds.right():
        rect.moveRight(bounds.right())
    if rect.bottom() > bounds.bottom():
        rect.moveBottom(bounds.bottom())
    return rect


def resized_rect(rect: QRectF, handle: str, delta: QPointF) -> QRectF:
    updated = QRectF(rect)
    if "left" in handle:
        updated.setLeft(updated.left() + delta.x())
    if "right" in handle:
        updated.setRight(updated.right() + delta.x())
    if "top" in handle:
        updated.setTop(updated.top() + delta.y())
    if "bottom" in handle:
        updated.setBottom(updated.bottom() + delta.y())

    updated = updated.normalized()
    if updated.width() < MIN_BOX_WIDTH:
        if "left" in handle:
            updated.setLeft(updated.right() - MIN_BOX_WIDTH)
        else:
            updated.setRight(updated.left() + MIN_BOX_WIDTH)
    if updated.height() < MIN_BOX_HEIGHT:
        if "top" in handle:
            updated.setTop(updated.bottom() - MIN_BOX_HEIGHT)
        else:
            updated.setBottom(updated.top() + MIN_BOX_HEIGHT)
    return updated


def map_node_to_rect(node: ImageNode, rect: QRectF) -> QPointF:
    return QPointF(rect.left() + node.x_ratio * rect.width(), rect.top() + node.y_ratio * rect.height())


def wrapped_line_segments(start: QPointF, end: QPointF, preview_rect: QRectF) -> list[tuple[QPointF, QPointF]]:
    width = preview_rect.width()
    local_start = QPointF(start.x() - preview_rect.left(), start.y() - preview_rect.top())
    local_end = QPointF(end.x() - preview_rect.left(), end.y() - preview_rect.top())
    candidate_x = min(
        (local_end.x() + offset for offset in (-width, 0.0, width)),
        key=lambda value: abs(value - local_start.x()),
    )

    adjusted_end = QPointF(candidate_x, local_end.y())
    if 0.0 <= adjusted_end.x() <= width:
        return [
            (
                QPointF(local_start.x() + preview_rect.left(), local_start.y() + preview_rect.top()),
                QPointF(adjusted_end.x() + preview_rect.left(), adjusted_end.y() + preview_rect.top()),
            )
        ]

    seam_x = 0.0 if adjusted_end.x() < 0.0 else width
    dx = adjusted_end.x() - local_start.x()
    if dx == 0:
        return []

    t = (seam_x - local_start.x()) / dx
    seam_y = local_start.y() + (adjusted_end.y() - local_start.y()) * t
    opposite_x = width if seam_x == 0.0 else 0.0
    wrapped_end_x = adjusted_end.x() + width if adjusted_end.x() < 0.0 else adjusted_end.x() - width

    return [
        (
            QPointF(local_start.x() + preview_rect.left(), local_start.y() + preview_rect.top()),
            QPointF(seam_x + preview_rect.left(), seam_y + preview_rect.top()),
        ),
        (
            QPointF(opposite_x + preview_rect.left(), seam_y + preview_rect.top()),
            QPointF(wrapped_end_x + preview_rect.left(), adjusted_end.y() + preview_rect.top()),
        ),
    ]


def width_for_count(count: int) -> float:
    return min(14.0, 1.2 + math.sqrt(max(count, 1)) * 1.15)


def width_for_relative_count(count: int, count_range: tuple[float, float]) -> float:
    minimum, maximum = count_range
    if maximum <= minimum:
        return (1.2 + 14.0) / 2.0
    t = (count - minimum) / (maximum - minimum)
    return 1.2 + max(0.0, min(1.0, t)) * (14.0 - 1.2)


def trim_line_segment(start: QPointF, end: QPointF, padding: float) -> tuple[QPointF, QPointF] | None:
    dx = end.x() - start.x()
    dy = end.y() - start.y()
    length = math.hypot(dx, dy)
    if length <= padding * 2:
        return None

    ux = dx / length
    uy = dy / length
    return (
        QPointF(start.x() + ux * padding, start.y() + uy * padding),
        QPointF(end.x() - ux * padding, end.y() - uy * padding),
    )


def color_for_distance(distance: float) -> QColor:
    green = QColor(44, 199, 111, 230)
    yellow = QColor(247, 206, 70, 230)
    red = QColor(230, 68, 68, 230)
    if not math.isfinite(distance):
        return red
    if distance <= 6.0:
        return lerp_color(green, yellow, distance / 6.0)
    return lerp_color(yellow, red, min((distance - 6.0) / 14.0, 1.0))


def color_for_relative_distance(distance: float, distance_range: tuple[float, float]) -> QColor:
    green = QColor(44, 199, 111, 230)
    yellow = QColor(247, 206, 70, 230)
    red = QColor(230, 68, 68, 230)
    if not math.isfinite(distance):
        return red

    minimum, maximum = distance_range
    if maximum <= minimum:
        return yellow

    t = max(0.0, min(1.0, (distance - minimum) / (maximum - minimum)))
    if t <= 0.5:
        return lerp_color(green, yellow, t / 0.5)
    return lerp_color(yellow, red, (t - 0.5) / 0.5)


def relationship_count_percentile_range(model: OverlayModel) -> tuple[float, float]:
    if not model.pairs:
        return 0.0, 0.0
    counts = [relationship.count for relationship in model.pairs]
    return percentile_range(counts)


def relationship_distance_percentile_range(model: OverlayModel) -> tuple[float, float]:
    finite_distances = [
        relationship.average_distance for relationship in model.pairs if math.isfinite(relationship.average_distance)
    ]
    if not finite_distances:
        return 0.0, 0.0
    return percentile_range(finite_distances)


def percentile_range(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0

    sorted_values = sorted(float(value) for value in values)
    if len(sorted_values) == 1:
        return sorted_values[0], sorted_values[0]

    return percentile(sorted_values, 0.05), percentile(sorted_values, 0.95)


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]

    position = (len(sorted_values) - 1) * fraction
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]

    weight = position - lower_index
    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    return lower_value + (upper_value - lower_value) * weight


def lerp_color(first: QColor, second: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        round(first.red() + (second.red() - first.red()) * t),
        round(first.green() + (second.green() - first.green()) * t),
        round(first.blue() + (second.blue() - first.blue()) * t),
        round(first.alpha() + (second.alpha() - first.alpha()) * t),
    )
