# Generated family -> card mapping (audit artifact)

## ★ 冻结路由对齐(2026-07-16):每族接到"历史获胜版本"

实际跑批路由以 `ROUTING.map` 为准(脚本读它,未列出的族走默认 `sf-<slug>.j2`)。
依据:`experiments/SkillFlow_campaign_scoreboard.md` + `SKILL_MEMORY_SUMMARY.md` 的"定版",
并用真实旧跑批 config→分数 台账逐族核实(in-sample,非泛化)。6 族被 override:

| 族 | 获胜版本 | 获胜分(证据) | 原 canonical 分 |
|---|---|---|---|
| embedded-data-repair | sf-embedded-data-repair-GK.j2 | 8/8 (sk-embedded-GK-s2) | 4/8 |
| weighted-risk-assessment | sf-weighted.j2 | 7/8 (sk-v5-weighted) | 0/8 |
| healthcare-cost-benefit-analysis | sf-universal-v2.j2 | ~6/9 (7,6,4) | 1/9 |
| distribution-center-auditing | sf-universal-v2.j2 | 5/8 (5,5,5) | 3/8 |
| inventory-finance-integration | sf-universal-v2.j2 | 4/8 (4,4,3) | 0/8 |
| industry-correlation-analysis | sf-universal-v2.j2 | 7/8 (7,7) | 6/8 |

其余族(production 8/9 / operational 4/8 / ppt 5/8 等)canonical 已是获胜版本,不动。
下列"family -> card"是更早的生成快照,已被上表 override 取代(仅保留作历史)。

---


- **Compensation-Scenario-Modeling** -> C2, C3, C16  (sf-compensation-scenario-modeling.j2, 9516 chars)
- **Cross-Format-Data-Reconciliation** -> (MACHINE only)  (sf-cross-format-data-reconciliation.j2, 5578 chars)
- **Distribution-Center-Auditing** -> C5, C7, C12  (sf-distribution-center-auditing.j2, 8942 chars)
- **DMAIC-Quality-Analysis** -> C8, C9  (sf-dmaic-quality-analysis.j2, 8246 chars)
- **Document-Fraud-Detection** -> (MACHINE only)  (sf-document-fraud-detection.j2, 5578 chars)
- **Embedded-Data-Repair** -> C3, C14  (sf-embedded-data-repair.j2, 9120 chars)
- **Financial-Statement-Rolling** -> C1  (sf-financial-statement-rolling.j2, 7375 chars)
- **Healthcare-Cost-Benefit-Analysis** -> C13, C21  (sf-healthcare-cost-benefit-analysis.j2, 7840 chars)
- **HWPX-Document-Automation** -> C22  (sf-hwpx-document-automation.j2, 6656 chars)
- **Industry-Correlation-Analysis** -> C19, C21  (sf-industry-correlation-analysis.j2, 8093 chars)
- **Inventory-&-Finance-Integration** -> C1  (sf-inventory-finance-integration.j2, 7375 chars)
- **Medical-Data-Standardization** -> C17  (sf-medical-data-standardization.j2, 6976 chars)
- **OCR-Data-Extraction** -> C20  (sf-ocr-data-extraction.j2, 7058 chars)
- **Operational-Recovery-Planning** -> C15, C16  (sf-operational-recovery-planning.j2, 7776 chars)
- **PPT-Formatting-Optimization** -> C14  (sf-ppt-formatting-optimization.j2, 10693 chars)
- **Production-Capacity-Planning** -> C15  (sf-production-capacity-planning.j2, 6663 chars)
- **Sales-Pivot-Analysis** -> C1, C2, C3, C4  (sf-sales-pivot-analysis.j2, 13045 chars)
- **SEC-13F-Financial-Analysis** -> C18  (sf-sec-13f-financial-analysis.j2, 6987 chars)
- **Supply-Chain-Replenishment** -> C1  (sf-supply-chain-replenishment.j2, 7375 chars)
- **Weighted-Risk-Assessment** -> C3  (sf-weighted-risk-assessment.j2, 7078 chars)
