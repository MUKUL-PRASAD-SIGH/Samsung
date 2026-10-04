package com.samsung.interruptible

import com.samsung.interruptible.data.AgentAction.GraphEdge
import com.samsung.interruptible.data.AgentAction.GraphNode
import com.samsung.interruptible.ui.GraphLayout
import com.samsung.interruptible.ui.Markdown
import com.samsung.interruptible.ui.Span
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class MarkdownTest {
    @Test fun boldAndCodeSpans() {
        assertEquals(
            listOf(Span("Found "), Span("2 flights", bold = true), Span(" via "), Span("DEL", code = true), Span(".")),
            Markdown.parseInline("Found **2 flights** via `DEL`."),
        )
    }

    @Test fun plainTextAndUnbalancedMarkersPassThrough() {
        assertEquals(listOf(Span("just text")), Markdown.parseInline("just text"))
        assertEquals(listOf(Span("a ** b")), Markdown.parseInline("a ** b"))
        assertEquals(listOf(Span("")), Markdown.parseInline(""))
    }

    @Test fun bulletsBecomeBulletsAndLinesArePreserved() {
        val parsed = Markdown.parse("Options:\n- cheapest: **IndiGo**\n* fastest\n• third")
        assertEquals(listOf(false, true, true, true), parsed.map { it.first })
        assertEquals(listOf(Span("cheapest: "), Span("IndiGo", bold = true)), parsed[1].second)
        assertEquals(listOf(Span("fastest")), parsed[2].second)
    }
}

class GraphLayoutTest {
    private fun n(id: String, type: String) = GraphNode(id, type, id)

    @Test fun turnsRunLeftToRightInOrderAndEntitiesHangUnderTheirTurn() {
        val nodes = listOf(n("t1", "turn"), n("e1", "entity"), n("t2", "turn"), n("e2", "entity"), n("e3", "entity"), n("art", "artifact"))
        val edges = listOf(
            GraphEdge("t1", "t2", "NEXT_TURN"), GraphEdge("t1", "e1", "REFERENCES"),
            GraphEdge("t2", "e2", "REFERENCES"), GraphEdge("t2", "e3", "REFERENCES"), GraphEdge("t2", "art", "PRODUCED"),
        )
        val l = GraphLayout.compute(nodes, edges)
        val p = l.positions
        assertTrue(p["t1"]!!.x < p["t2"]!!.x && p["t1"]!!.y == p["t2"]!!.y)
        assertEquals(p["t1"]!!.x, p["e1"]!!.x, 0f)
        assertEquals(p["t2"]!!.x, p["e2"]!!.x, 0f)
        assertTrue(p["e1"]!!.y > p["t1"]!!.y)
        assertEquals("three nodes under t2 stack in distinct rows", 3, setOf(p["e2"]!!.y, p["e3"]!!.y, p["art"]!!.y).size)
        assertTrue(l.width >= p.values.maxOf { it.x } && l.height >= p.values.maxOf { it.y })
    }

    @Test fun anEntityConnectedInEitherDirectionIsAnchored() {
        val l = GraphLayout.compute(listOf(n("t1", "turn"), n("t2", "turn"), n("e", "entity")), listOf(GraphEdge("e", "t2", "REFERENCES")))
        assertEquals(l.positions["t2"]!!.x, l.positions["e"]!!.x, 0f)
    }

    @Test fun orphansAndEmptyGraphsDoNotCrash() {
        val l = GraphLayout.compute(listOf(n("t1", "turn"), n("lonely", "entity")), emptyList())
        assertTrue(l.positions.containsKey("lonely"))
        val empty = GraphLayout.compute(emptyList(), emptyList())
        assertTrue(empty.positions.isEmpty() && empty.width > 0 && empty.height > 0)
        assertTrue(GraphLayout.compute(listOf(n("only", "entity")), emptyList()).positions.containsKey("only"))
    }

    @Test fun layoutIsDeterministic() {
        val nodes = listOf(n("t1", "turn"), n("e1", "entity"), n("e2", "entity"))
        val edges = listOf(GraphEdge("t1", "e1", "REFERENCES"), GraphEdge("t1", "e2", "REFERENCES"))
        assertEquals(GraphLayout.compute(nodes, edges), GraphLayout.compute(nodes, edges))
    }
}
