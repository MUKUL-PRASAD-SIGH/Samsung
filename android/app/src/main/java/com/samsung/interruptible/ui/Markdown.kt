package com.samsung.interruptible.ui

/** A run of text with the inline style the agent's markdown asked for. */
data class Span(val text: String, val bold: Boolean = false, val code: Boolean = false)

/**
 * The agent writes light markdown (**bold**, `code`, "- " bullets). Compose has no markdown renderer built in and a
 * full one is overkill for chat bubbles, so this handles exactly those: pure, so it is unit-tested.
 */
object Markdown {
    private val inline = Regex("""\*\*(.+?)\*\*|`([^`]+)`""")

    fun parseInline(line: String): List<Span> {
        val spans = mutableListOf<Span>()
        var last = 0
        for (m in inline.findAll(line)) {
            if (m.range.first > last) spans += Span(line.substring(last, m.range.first))
            val bold = m.groups[1]
            val code = m.groups[2]
            spans += if (bold != null) Span(bold.value, bold = true) else Span(code!!.value, code = true)
            last = m.range.last + 1
        }
        if (last < line.length) spans += Span(line.substring(last))
        return spans.ifEmpty { listOf(Span(line)) }
    }

    /** Lines of the reply, each with a bullet flag; markdown list markers become a real bullet. */
    fun parse(text: String): List<Pair<Boolean, List<Span>>> = text.lines().map { raw ->
        val bullet = raw.trimStart().let { it.startsWith("- ") || it.startsWith("* ") || it.startsWith("• ") }
        val body = if (bullet) raw.trimStart().drop(2) else raw
        bullet to parseInline(body)
    }
}
