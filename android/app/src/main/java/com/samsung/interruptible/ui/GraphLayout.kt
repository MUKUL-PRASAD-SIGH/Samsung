package com.samsung.interruptible.ui

import com.samsung.interruptible.data.AgentAction.GraphEdge
import com.samsung.interruptible.data.AgentAction.GraphNode

data class Pt(val x: Float, val y: Float)

data class GraphLayoutResult(val positions: Map<String, Pt>, val width: Float, val height: Float)

/**
 * Lays the cognitive graph out for a phone: conversation turns run left to right in the order they happened; each turn's
 * entities and artifacts hang below it. Deterministic and pure, so the layout is unit-tested.
 */
object GraphLayout {
    const val COL_WIDTH = 190f
    const val ROW_HEIGHT = 70f
    const val PAD = 16f

    fun compute(nodes: List<GraphNode>, edges: List<GraphEdge>): GraphLayoutResult {
        val turns = nodes.filter { it.nodeType == "turn" }
        val column = turns.withIndex().associate { (i, n) -> n.id to i }
        val positions = LinkedHashMap<String, Pt>()
        turns.forEachIndexed { i, n -> positions[n.id] = Pt(PAD + i * COL_WIDTH, PAD) }

        // Anchor every other node under a turn it is connected to (the turn that introduced or produced it).
        val rowsUsed = HashMap<Int, Int>()
        var orphanCol = 0
        for (n in nodes.filter { it.nodeType != "turn" }) {
            val anchor = edges.firstNotNullOfOrNull { e ->
                when {
                    e.source == n.id && e.target in column -> column[e.target]
                    e.target == n.id && e.source in column -> column[e.source]
                    else -> null
                }
            } ?: (orphanCol++ % maxOf(1, turns.size))
            val row = (rowsUsed[anchor] ?: 0) + 1
            rowsUsed[anchor] = row
            positions[n.id] = Pt(PAD + anchor * COL_WIDTH, PAD + row * ROW_HEIGHT)
        }
        val width = PAD * 2 + maxOf(1, turns.size) * COL_WIDTH
        val height = PAD * 2 + ((rowsUsed.values.maxOrNull() ?: 0) + 1) * ROW_HEIGHT
        return GraphLayoutResult(positions, width, height)
    }
}
