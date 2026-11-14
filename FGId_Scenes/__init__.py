# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene properties and thumbnails for the IngeTrazo Scenes tray."""
from copy import copy

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QWidget)

from core.outliner import rename_scene
from core.saved_views import SavedView
from views.tray import fit_rows, tr

KEY = "fgid_scene_properties"
PROPERTIES = {
    "camera": "Camera Location",
    "geometry": "Top-Level Hidden Geometry",
    "objects": "Hidden Objects",
    "tags": "Visible Tags",
    "cuts": "Active Section Planes",
    "style": "Display Style",
    "shadows": "Shadow Settings",
    "georef": "Geographic Layers",
    "axes": "Show Axes",
}
CAMERA_FIELDS = ("target", "distance", "yaw", "pitch", "fov_deg", "perspective", "two_point")


def metadata(view):
    return (view.section or {}).get(KEY, {})


def geometry_key(points):
    return repr(sorted(tuple(p.toTuple()) for p in points))


def capture_geometry(scene):
    mesh = scene.loose_mesh
    return {
        "faces": [geometry_key(face.vertices) for face in mesh.faces if face.attrs.get("hidden")],
        "edges": [geometry_key((edge.a, edge.b)) for edge in mesh.edges if edge.hidden],
    }


def restore_geometry(scene, snapshot):
    mesh = scene.loose_mesh
    faces, edges = set(snapshot["faces"]), set(snapshot["edges"])
    changed = False
    for face in mesh.faces:
        hidden = bool(faces) and geometry_key(face.vertices) in faces
        if hidden != bool(face.attrs.get("hidden")):
            changed = True
            if hidden:
                face.attrs["hidden"] = True
            else:
                face.attrs.pop("hidden", None)
    for edge in mesh.edges:
        hidden = bool(edges) and geometry_key((edge.a, edge.b)) in edges
        if hidden != edge.hidden:
            changed = True
            edge.hidden = hidden
    if changed:
        scene.version += 1


def install_recall():
    original = SavedView.apply
    if getattr(original, "_fgid_scenes", False):
        return

    def apply(view, scene, camera):
        data = metadata(view)
        if not data:
            return original(view, scene, camera)
        enabled = data.get("properties", {})
        selected = copy(view)
        if not enabled.get("camera", True):
            selected.target = camera.target.toTuple()
            for field in CAMERA_FIELDS[1:]:
                setattr(selected, field, getattr(camera, field))
        if not enabled.get("tags", True):
            selected.hidden_layers = [layer.name for layer in scene.layers if not layer.visible]
        for prop, field in (("style", "style"), ("shadows", "shadows"),
                            ("georef", "georef"), ("objects", "hidden_objects"),
                            ("cuts", "section")):
            if not enabled.get(prop, True):
                setattr(selected, field, None)
        if view.hidden_shown:
            selected.hidden_shown = dict(view.hidden_shown)
            if not enabled.get("objects", True):
                selected.hidden_shown["objects"] = scene.show_hidden_objects
            if not enabled.get("geometry", True):
                selected.hidden_shown["geometry"] = scene.show_hidden_geometry
        # Respect the scene flags imported with the .skp file, intersected with our checkboxes.
        skp = (view.section or {}).get("fgid_skp_state")
        if skp:
            selected.section = dict(view.section)
            state = dict(skp)
            props = {"camera": "camera", "tags": "tags", "cuts": "cuts", "style": "style",
                     "hidden_geometry": "geometry", "hidden_objects": "objects"}
            state["use"] = {flag: value and enabled.get(props.get(flag), True)
                            for flag, value in skp.get("use", {}).items()}
            selected.section["fgid_skp_state"] = state
        original(selected, scene, camera)
        if enabled.get("geometry", True) and "geometry" in data and scene.edit_group is None:
            restore_geometry(scene, data["geometry"])
        if enabled.get("axes", True) and "axes" in data:
            scene.show_axes = data["axes"]

    apply._fgid_scenes = True
    SavedView.apply = apply


class SceneControls(QWidget):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.syncing = False
        self.play_scene = None
        self.timer = QTimer(self, interval=2000)
        self.timer.timeout.connect(self.advance)
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.description = QLineEdit()
        self.include = QCheckBox("Include in animation")
        form.addRow(self.include)
        form.addRow("Name", self.name)
        form.addRow("Description", self.description)
        self.checks = {}
        for key, label in PROPERTIES.items():
            box = QCheckBox(label)
            self.checks[key] = box
            form.addRow(box)
            box.toggled.connect(self.save)
        self.name.editingFinished.connect(self.rename)
        self.description.editingFinished.connect(self.save)
        self.include.toggled.connect(self.save)
        panel.list.currentItemChanged.connect(self.sync)
        panel.list.setIconSize(QSize(80, 54))

        row = QHBoxLayout()
        for label, callback in (("↑", lambda: self.move(-1)), ("↓", lambda: self.move(1)),
                                ("Play / Stop", self.play)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        panel.layout().insertLayout(0, row)
        panel.layout().addWidget(self)

        refresh = panel.refresh
        def refresh_with_details():
            current = self.current()
            refresh()
            panel._updating = True
            try:
                for index in range(panel.list.count()):
                    item = panel.list.item(index)
                    view = item.data(Qt.UserRole)
                    data = metadata(view)
                    if data.get("thumbnail"):
                        pixmap = QPixmap()
                        pixmap.loadFromData(QByteArray.fromBase64(data["thumbnail"].encode("ascii")))
                        item.setIcon(QIcon(pixmap))
                    description = data.get("description", "")
                    item.setText(view.name + ("\n" + description if description else ""))
                    item.setToolTip(description)
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    if view is current:
                        panel.list.setCurrentItem(item)
            finally:
                panel._updating = False
            fit_rows(panel.list)
            self.sync()
        panel.refresh = refresh_with_details
        self.refresh = refresh_with_details
        add, update = tr("+ Scene"), tr("Update")
        for button in panel.findChildren(QPushButton):
            # The native controls own add/update; a second slot captures our fields.
            if button.text() in (add, update):
                button.clicked.connect(self.after_add if button.text() == add else self.capture)
        self.refresh()

    def current(self):
        item = self.panel.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def sync(self, *_):
        view = self.current()
        self.setEnabled(view is not None)
        data = metadata(view) if view else {}
        self.syncing = True
        try:
            self.name.setText(view.name if view else "")
            self.description.setText(data.get("description", ""))
            self.include.setChecked(data.get("animation", True))
            for key, box in self.checks.items():
                box.setChecked(data.get("properties", {}).get(key, True))
        finally:
            self.syncing = False

    def save(self, *_):
        view = self.current()
        if self.syncing or view is None:
            return
        data = dict(metadata(view))
        data.update(description=self.description.text(), animation=self.include.isChecked(),
                    properties={key: box.isChecked() for key, box in self.checks.items()})
        view.section = {**(view.section or {}), KEY: data}
        item = self.panel.list.currentItem()
        self.panel._updating = True
        try:
            description = data["description"]
            item.setText(view.name + ("\n" + description if description else ""))
            item.setToolTip(description)
        finally:
            self.panel._updating = False
        self.panel._touch()

    def rename(self):
        view = self.current()
        name = self.name.text().strip()
        if view and name and name != view.name:
            rename_scene(self.panel._scene(), view, name)
            self.refresh()
            self.panel._touch()

    def after_add(self):
        self.panel.list.setCurrentRow(self.panel.list.count() - 1)
        self.capture()

    def capture(self):
        view = self.current()
        if view is None:
            return
        self.save()
        scene = self.panel._scene()
        data = metadata(view)
        if scene.edit_group is None and self.checks["geometry"].isChecked():
            data["geometry"] = capture_geometry(scene)
        data["axes"] = scene.show_axes
        viewport = self.panel._window.viewport
        image = viewport.grabFramebuffer()
        if not image.isNull():
            image = image.scaled(128, 84, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            bytes_ = QByteArray()
            buffer = QBuffer(bytes_)
            buffer.open(QIODevice.WriteOnly)
            if image.save(buffer, "PNG"):
                data["thumbnail"] = bytes(bytes_.toBase64()).decode("ascii")
        self.refresh()

    def move(self, delta):
        row = self.panel.list.currentRow()
        views = self.panel._scene().saved_views
        destination = row + delta
        if row >= 0 and 0 <= destination < len(views):
            views.insert(destination, views.pop(row))
            self.refresh()
            self.panel._touch()

    def play(self):
        if self.timer.isActive():
            self.timer.stop()
            return
        self.play_scene = self.panel._scene()
        self.play_index = -1
        self.advance()
        if self.play_index >= 0:
            self.timer.start()

    def advance(self):
        if self.play_scene is not self.panel._scene():
            self.timer.stop()
            return
        for offset in range(self.play_index + 1, self.panel.list.count()):
            item = self.panel.list.item(offset)
            if metadata(item.data(Qt.UserRole)).get("animation", True):
                self.play_index = offset
                self.panel.list.setCurrentItem(item)
                self.panel._on_activate(item)
                return
        self.timer.stop()
        self.play_index = -1


def setup(app):
    install_recall()
    panel = app.window.tray.scenes
    if not hasattr(panel, "_fgid_scene_controls"):
        panel._fgid_scene_controls = SceneControls(panel)
