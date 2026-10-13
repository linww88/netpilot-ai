import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

class ReportGenerator:
    def __init__(self, data_dir="./data"):
        self.data_dir = data_dir
        # 确保报告目录存在（全新克隆的仓库不会有 data/ 目录，需自动创建）
        os.makedirs(self.data_dir, exist_ok=True)

    def generate_json_report(self, task_name, task_result, status):
        report = {
            "task": task_name,
            "timestamp": datetime.now().isoformat(),
            "status": status,
            "result": task_result
        }
        filename = f"{task_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(f"{self.data_dir}/{filename}", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info(f"Report generated: {filename}")

    def generate_text_report(self, task_name, task_result):
        report_str = f"Task: {task_name}\nTimestamp: {datetime.now()}\nResult: {task_result}"
        filename = f"{task_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(f"{self.data_dir}/{filename}", "w", encoding="utf-8") as f:
            f.write(report_str)
        logger.info(f"Report generated: {filename}")
