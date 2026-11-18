"""How poser reads text a person writes: UTF-8, with or without a leading byte-order mark.

Some Windows tools put the mark first, such as Windows PowerShell 5.1's `Set-Content -Encoding
utf8` and Notepad's "UTF-8 with BOM". `tomllib` and `json` refuse it, so every reader of a
hand-written file, such as `poser.toml`, a song, a saved sound, or `.gitignore`, decodes with
`AUTHORED_ENCODING`. poser writes every file without the mark. Files only poser writes, such as
render records, are read as plain UTF-8, so a mark there is an error.
"""

AUTHORED_ENCODING = "utf-8-sig"
"""The codec for a hand-written file: UTF-8, dropping one leading byte-order mark."""

BYTE_ORDER_MARK = "\ufeff"
"""The byte-order mark as text, which UTF-8 encodes as `EF BB BF`."""
