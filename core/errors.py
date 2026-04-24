# Portions derived from BlenderGIS (https://github.com/domlysz/BlenderGIS)
# Copyright (C) domlysz — GNU General Public License v3.0


class OverlapError(Exception):
    def __str__(self):
        return "Non-overlapping data"


class ReprojError(Exception):
    def __init__(self, value=""):
        self.value = value

    def __str__(self):
        return repr(self.value)


class ApiKeyError(Exception):
    def __str__(self):
        return "Missing or invalid API key"


class CRSError(Exception):
    def __init__(self, value=""):
        self.value = value

    def __str__(self):
        return repr(self.value)


class ParseError(Exception):
    def __init__(self, value=""):
        self.value = value

    def __str__(self):
        return repr(self.value)


class FormatError(Exception):
    def __init__(self, value=""):
        self.value = value

    def __str__(self):
        return repr(self.value)
