"""PantheraIDS restructuring tool.

Streams a camera-trap image tree into ``DEST/Check/<CAxxx>/<MMDDYY>/`` with
bounded memory, a resumable SQLite state database and no exiftool dependency.
See DESIGN.md.
"""

__version__ = "0.1.0"
