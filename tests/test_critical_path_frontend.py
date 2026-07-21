from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CriticalPathFrontendTests(unittest.TestCase):
    def test_critical_path_overlay_and_cross_view_highlight_are_present(self) -> None:
        html = (ROOT / "business_app" / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "business_app" / "static" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "business_app" / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="criticalPathOverlay"', html)
        self.assertIn("function showCriticalPathInfo", javascript)
        self.assertIn("function selectCriticalPath", javascript)
        self.assertIn("function applyCriticalPathHighlight", javascript)
        self.assertIn("criticalPathItems:new Map()", javascript)
        self.assertIn("criticalPathsByProcessId:new Map()", javascript)
        self.assertIn("function criticalPathOverview", javascript)
        self.assertIn("structural_path_ids", javascript)
        self.assertIn("delivery_path_ids", javascript)
        self.assertIn("resource_path_ids", javascript)
        self.assertIn("critical_override_required", javascript)
        self.assertIn("critical_override_authorized", javascript)
        self.assertIn("function criticalLabelDots", javascript)
        critical_ids = next(
            line for line in javascript.splitlines() if line.startswith("function criticalPathIds")
        )
        critical_dots = next(
            line for line in javascript.splitlines() if line.startswith("function criticalLabelDots")
        )
        critical_marker = next(
            line for line in javascript.splitlines() if line.startswith("function criticalMarker")
        )
        gantt_bar = next(
            line for line in javascript.splitlines() if line.startswith("function ganttBar")
        )
        order_summary = next(
            line for line in javascript.splitlines() if line.startswith("function orderCriticalSummary")
        )
        self.assertNotIn("structural_path_ids", critical_ids)
        self.assertNotIn("structural", critical_dots)
        self.assertNotIn(">S<", critical_marker)
        self.assertNotIn("gantt-critical-structural", gantt_bar)
        self.assertNotIn('<i class="structural">', order_summary)
        self.assertIn('<i class="delivery">D${delivery.length}</i>', order_summary)
        self.assertIn('<i class="resource">R${resource.length}</i>', order_summary)
        self.assertIn("function criticalGanttLegend", javascript)
        self.assertIn("D</i><span><strong>交付关键", javascript)
        self.assertIn("R</i><span><strong>资源关键", javascript)
        self.assertIn("function sortedOrderGroupEntries", javascript)
        self.assertIn("sortedOrderGroupEntries(grouped).map", javascript)
        self.assertNotIn("三类关键性独立管理", javascript)
        self.assertIn('critical-overview compact', javascript)
        self.assertNotIn("function criticalPathPayload", javascript)
        self.assertIn(".gantt-critical-structural", styles)
        self.assertIn(".gantt-critical-delivery", styles)
        self.assertIn(".gantt-critical-resource", styles)
        self.assertIn(".gantt-path-selected", styles)
        self.assertIn(".critical-marker.delivery.negative", styles)
        premium = (ROOT / "business_app" / "static" / "premium.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".critical-gantt-legend", premium)
        self.assertIn(".gantt .critical-marker.structural", premium)


if __name__ == "__main__":
    unittest.main()
