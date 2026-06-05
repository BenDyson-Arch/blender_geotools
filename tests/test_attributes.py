import json

from addons.data.geojson.attributes import (
    read_mesh_attributes,
    write_feature_properties,
    write_mesh_attributes,
)


class _FakeAttributeElement:
    def __init__(self):
        self.value = None
        self.vector = None


class _FakeAttribute:
    def __init__(self, name, data_type, domain, size=1):
        self.name = name
        self.data_type = data_type
        self.domain = domain
        self.data = [_FakeAttributeElement() for _ in range(size)]


class _FakeAttributes:
    def __init__(self, size=1):
        self._attrs = {}
        self._size = size

    def __contains__(self, name):
        return name in self._attrs

    def __getitem__(self, name):
        return self._attrs[name]

    def __iter__(self):
        return iter(self._attrs.values())

    def new(self, name, type, domain):
        attr = _FakeAttribute(name, type, domain, size=self._size)
        self._attrs[name] = attr
        return attr

    def remove(self, attr):
        self._attrs.pop(attr.name, None)


class _FakeMesh:
    def __init__(self, size=1):
        self.attributes = _FakeAttributes(size=size)


class _FakeObject(dict):
    pass


def test_write_mesh_attributes_encodes_string_values_as_bytes():
    mesh = _FakeMesh(size=2)

    written = write_mesh_attributes(mesh, {"name": "PYSU"})

    assert written == {"name"}
    attr = mesh.attributes["name"]
    assert attr.data_type == "STRING"
    assert [element.value for element in attr.data] == [b"PYSU", b"PYSU"]


def test_write_mesh_attributes_round_trips_json_like_values():
    mesh = _FakeMesh()
    properties = {"meta": {"layer": "parcel", "count": 3}}

    written = write_mesh_attributes(mesh, properties)

    assert written == {"meta"}
    stored = mesh.attributes["meta"].data[0].value
    assert isinstance(stored, bytes)
    assert json.loads(stored.decode("utf-8")) == properties["meta"]
    assert read_mesh_attributes(mesh) == {"meta": json.dumps(properties["meta"])}


def test_write_feature_properties_can_skip_mesh_attributes():
    obj = _FakeObject()
    mesh = _FakeMesh()
    properties = {"name": "PYSU", "value": 3.5}

    write_feature_properties(
        obj,
        mesh,
        properties,
        store_custom_props=False,
        write_attributes=False,
        auto_normal=False,
        normal_prop="",
    )

    assert list(mesh.attributes) == []
    assert obj == {"name": "PYSU", "value": 3.5}
