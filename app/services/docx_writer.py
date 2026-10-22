"""Minimal DOCX writer (Office Open XML) using only the standard library.

Word-compatible output requires: ``[Content_Types].xml``, ``_rels/.rels``,
``word/document.xml`` and ``word/_rels/document.xml.rels``. That is all this
writer produces — enough for a valid, editable manuscript with headings,
paragraphs, bullet lists and tables.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/><w:pPr><w:spacing w:after="240"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="36"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/><w:pPr><w:spacing w:before="280" w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/><w:pPr><w:spacing w:before="200" w:after="80"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="24"/></w:rPr>
  </w:style>
</w:styles>
"""


@dataclass
class DocxDocument:
    """Accumulates simple block elements and renders a .docx byte string."""

    blocks: list[str] = field(default_factory=list)

    def title(self, text: str) -> None:
        self.paragraph(text, style="Title", bold=True, size=36)

    def heading(self, text: str, level: int = 1) -> None:
        if level == 1:
            self.paragraph(text, style="Heading1", bold=True, size=28)
        else:
            self.paragraph(text, style="Heading2", bold=True, size=24)

    def paragraph(self, text: str, *, style: str = "", bold: bool = False, size: int = 22, italic: bool = False) -> None:
        properties = f'<w:pStyle w:val="{style}"/>' if style else ""
        run_properties = (
            f"<w:rPr>{'<w:b/>' if bold else ''}{'<w:i/>' if italic else ''}"
            f'<w:sz w:val="{size}"/></w:rPr>'
        )
        self.blocks.append(
            f"<w:p><w:pPr>{properties}</w:pPr>"
            f"<w:r>{run_properties}<w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>"
        )

    def bullet(self, text: str, level: int = 0) -> None:
        indent = 200 + level * 200
        self.blocks.append(
            '<w:p><w:pPr><w:ind w:left="{}" w:hanging="180"/></w:pPr>'
            '<w:r><w:rPr><w:sz w:val="22"/></w:rPr>'
            '<w:t xml:space="preserve">• {}</w:t></w:r></w:p>'.format(indent, escape(text))
        )

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        def cell(text: str, header: bool = False) -> str:
            bold = "<w:b/>" if header else ""
            return (
                "<w:tc><w:tcPr><w:tcW w:w=\"2400\" w:type=\"dxa\"/></w:tcPr>"
                f"<w:p><w:r><w:rPr>{bold}<w:sz w:val=\"20\"/></w:rPr>"
                f"<w:t xml:space=\"preserve\">{escape(str(text))}</w:t></w:r></w:p></w:tc>"
            )

        borders = (
            "<w:tblBorders>"
            + "".join(
                f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
                for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
            )
            + "</w:tblBorders>"
        )
        header_row = "<w:tr>" + "".join(cell(value, header=True) for value in headers) + "</w:tr>"
        body_rows = "".join(
            "<w:tr>" + "".join(cell(value) for value in row) + "</w:tr>" for row in rows
        )
        self.blocks.append(f"<w:tbl><w:tblPr>{borders}</w:tblPr>{header_row}{body_rows}</w:tbl>")
        self.blocks.append("<w:p/>")

    def render(self) -> bytes:
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>" + "".join(self.blocks) + "<w:sectPr/></w:body></w:document>"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("_rels/.rels", RELS)
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
            archive.writestr("word/styles.xml", STYLES)
        return buffer.getvalue()
