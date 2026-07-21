from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlanModalPerformanceTests(unittest.TestCase):
    def test_plan_modal_uses_lazy_views_collapsed_orders_and_paged_details(self) -> None:
        javascript = (ROOT / "business_app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        styles = (ROOT / "business_app" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        premium = (ROOT / "business_app" / "static" / "premium.css").read_text(
            encoding="utf-8"
        )

        self.assertIn("planVersionView", javascript)
        self.assertIn("detailPageSize:50", javascript)
        self.assertIn("function renderPlanGanttView", javascript)
        self.assertIn("function renderPlanOrderGantt", javascript)
        self.assertIn("function togglePlanOrderGantt", javascript)
        self.assertIn("function renderPlanSchedulePage", javascript)
        self.assertIn("requestAnimationFrame", javascript)
        self.assertIn('data-plan-view-content="machine"', javascript)
        self.assertIn('data-plan-view-content="worker"', javascript)
        self.assertIn('data-plan-view-content="detail"', javascript)
        self.assertIn('data-plan-view-content="load"', javascript)
        self.assertIn("function renderMachineLoadChart", javascript)
        self.assertIn("设备负荷率 = 设备计划占用工时 ÷ 计划周期内设备可用工时 × 100%", javascript)
        self.assertIn("machineLoadContext", javascript)
        self.assertIn("负荷占比", javascript)
        self.assertIn("日历负荷率", javascript)
        self.assertIn("calendarInfo.machine_name", javascript)
        self.assertIn("设备总可用工时", javascript)
        self.assertIn("排程明细", javascript)
        self.assertIn("设备负荷图", javascript)
        self.assertNotIn('<section class="plan-detail-section">', javascript)
        self.assertNotIn('data-tooltip="${ganttTooltipPayload(item)}"', javascript)
        self.assertIn(".plan-table-pagination", styles)
        self.assertIn(".plan-view-placeholder", styles)
        self.assertIn(".machine-load-list", premium)
        self.assertIn("width: min(1440px, 98vw)", premium)
        self.assertIn("grid-template-columns: repeat(6, minmax(0, 1fr))", premium)
        self.assertIn(".plan-modal .version-parameter-grid", premium)
        self.assertIn("grid-template-columns: repeat(7, minmax(0, 1fr))", premium)
        self.assertIn(".plan-modal .critical-overview.compact", premium)


if __name__ == "__main__":
    unittest.main()
