"""Render published Markdown without active HTML, images, or unsafe links."""

from PySide6.QtGui import QFont, QTextCursor, QTextDocument, QTextFormat

from solin.core.remote.urls import is_safe_remote_url


def release_notes_html(markdown: str) -> str:
    document = QTextDocument()
    font = QFont()
    font.setPixelSize(14)
    document.setDefaultFont(font)
    document.setMarkdown(
        markdown,
        QTextDocument.MarkdownFeature.MarkdownDialectGitHub
        | QTextDocument.MarkdownFeature.MarkdownNoHTML,
    )
    # Work backwards so replacing an image cannot invalidate later ranges.
    fragments = []
    block = document.begin()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid():
                fragments.append((fragment.position(), fragment.length(), fragment.charFormat()))
            iterator += 1
        block = block.next()
    for position, length, char_format in reversed(fragments):
        cursor = QTextCursor(document)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        if char_format.isImageFormat():
            cursor.removeSelectedText()
        elif char_format.isAnchor():
            # Color belongs to the consuming theme, not the palette of the
            # worker thread that parsed the Markdown.
            char_format.clearProperty(QTextFormat.Property.ForegroundBrush)
            if not is_safe_remote_url(char_format.anchorHref()):
                char_format.setAnchor(False)
                char_format.setAnchorHref("")
                char_format.clearProperty(QTextFormat.Property.TextUnderlineStyle)
            cursor.setCharFormat(char_format)
    return document.toHtml()
