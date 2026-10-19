"""CarTest core — 用例数据源

支持从 Excel / CSV / TXT / JSON 加载测试用例。

来源：``CarVoiceTestTool/data_source.py`` 平移（2026-09-14）。

相对原实现的两处调整（均为消除反模式，不改解析逻辑）：
  1. **移除模块级 ``logging.basicConfig()``**——库代码不应配置全局日志。
     日志由调用方（app / cli）统一配置；本模块只取 ``getLogger(__name__)``。
  2. 删除未使用的 ``typing.Iterator`` 导入。

已知待办（P2）：``export_results`` 属报表职责，应外迁到 ``core/report.py``。
P1 先原样保留，避免改变行为。
"""
from __future__ import annotations

import csv
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TestCase:
    """测试用例"""
    id: str                          # 用例ID
    query: str                       # 语音灌入文本
    expected_keywords: list[str] = field(default_factory=list)  # 期望结果关键词
    expected_intent: str = ""        # 期望意图类型
    category: str = ""               # 用例分类/标签
    priority: str = "P1"             # 优先级
    remark: str = ""                 # 备注

    # 运行时结果
    status: str = "pending"          # pending | running | pass | fail | skip | error
    inject_time: str = ""            # 灌入时间（主机本地 MM-DD HH:MM:SS）
    actual_result: str = ""          # 实际结果描述
    asr_text: str = ""               # ASR识别文本
    intent_type: str = ""            # 实际意图类型
    response_time: float = 0.0       # 响应时间(秒) - 兼容旧代码
    asr_response_time: float = 0.0   # ASR响应时间(秒)
    sys_response_time: float = 0.0   # 系统响应时间(秒)
    intent_response_time: float = 0.0  # 意图识别时间(秒)
    tts_response_time: float = 0.0   # TTS播放时间(秒)
    e2e_response_time: float = 0.0   # 端到端响应时间(秒)
    log_snippet: str = ""            # 关键日志片段
    error_msg: str = ""              # 错误信息
    prev_session: bool = False       # 结果是否来自上一次会话（断点续跑回显）

    def reset(self) -> None:
        self.status = "pending"
        self.inject_time = ""
        self.actual_result = ""
        self.asr_text = ""
        self.intent_type = ""
        self.response_time = 0.0
        self.asr_response_time = 0.0
        self.sys_response_time = 0.0
        self.intent_response_time = 0.0
        self.tts_response_time = 0.0
        self.e2e_response_time = 0.0
        self.log_snippet = ""
        self.error_msg = ""
        self.prev_session = False


class DataSource:
    """数据源管理器"""

    def __init__(self) -> None:
        self._cases: list[TestCase] = []
        self._source_path: Path | None = None
        self._split_multi_round: bool = False  # 是否拆分 Q1:/Q2: 多轮 query

    @property
    def split_multi_round(self) -> bool:
        """多轮拆分开关（Q1:/Q2: 前缀）"""
        return self._split_multi_round

    @split_multi_round.setter
    def split_multi_round(self, value: bool) -> None:
        self._split_multi_round = bool(value)

    @property
    def cases(self) -> list[TestCase]:
        return self._cases

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    @property
    def count(self) -> int:
        return len(self._cases)

    def load_file(self, file_path: str | Path) -> tuple[bool, str, int]:
        """加载测试用例文件"""
        path = Path(file_path)
        logger.info(f"开始加载文件: {path}")
        logger.info(f"文件绝对路径: {path.absolute()}")
        logger.info(f"文件是否存在: {path.exists()}")

        if not path.exists():
            logger.error(f"文件不存在: {path}")
            return False, f"文件不存在: {path}", 0

        suffix = path.suffix.lower()
        logger.info(f"文件后缀: {suffix}")
        try:
            if suffix in (".xlsx", ".xls"):
                return self._load_excel(path)
            elif suffix == ".csv":
                return self._load_csv(path)
            elif suffix in (".txt", ".text"):
                return self._load_txt(path)
            elif suffix == ".json":
                return self._load_json(path)
            else:
                logger.error(f"不支持的文件格式: {suffix}")
                return False, f"不支持的文件格式: {suffix}", 0
        except Exception as e:  # noqa: BLE001
            logger.exception(f"加载文件异常: {e}")
            return False, f"加载失败: {e}", 0

    def _load_excel(self, path: Path) -> tuple[bool, str, int]:
        """从 Excel 加载，支持多轮测试格式"""
        logger.info(f"开始加载 Excel 文件: {path}")
        try:
            import openpyxl
            logger.debug("openpyxl 导入成功")
        except ImportError:
            logger.error("openpyxl 未安装")
            return False, "需要安装 openpyxl: pip install openpyxl", 0

        logger.debug("正在打开 Excel 文件...")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        logger.info(f"活动工作表: {ws.title}")

        rows = list(ws.iter_rows(values_only=True))
        logger.info(f"读取到 {len(rows)} 行数据")

        if not rows:
            logger.warning("Excel 文件为空")
            wb.close()
            return False, "Excel 文件为空", 0

        # 解析表头
        raw_headers = rows[0]
        logger.info(f"原始表头: {raw_headers}")
        headers = [str(h).strip().lower() if h else "" for h in raw_headers]
        logger.info(f"处理后表头: {headers}")

        # 检测多轮测试格式
        is_multi_round = self._detect_multi_round_format(headers)
        logger.info(f"多轮测试格式: {is_multi_round}")

        self._cases.clear()
        skipped = 0

        if is_multi_round:
            # 多轮测试格式：展开每行的多组 Query
            case_idx = 1
            for row_idx, row in enumerate(rows[1:], start=1):
                if not row or not any(row):
                    skipped += 1
                    continue

                # 提取基础信息
                base_id = str(row[0]).strip() if row[0] else f"R{row_idx}"
                base_category = str(row[2]).strip() if len(row) > 2 and row[2] else ""
                base_remark = str(row[3]).strip() if len(row) > 3 and row[3] else ""

                # 展开5组Query
                # 列结构: [序号0, 测试点1, 前置条件2, 第N模块, 第NQuery, 第N期望结果]
                #         第N模块=3+(N-1)*3, 第NQuery=4+(N-1)*3, 第N期望结果=5+(N-1)*3
                for round_num in range(1, 6):
                    module_col = 3 + (round_num - 1) * 3      # 3, 6, 9, 12, 15
                    query_col = module_col + 1                # 4, 7, 10, 13, 16
                    expected_col = query_col + 1              # 5, 8, 11, 14, 17

                    if query_col < len(row) and row[query_col]:
                        query = str(row[query_col]).strip()
                        expected = str(row[expected_col]).strip() if expected_col < len(row) and row[expected_col] else ""
                        module = str(row[module_col]).strip() if module_col < len(row) and row[module_col] else ""

                        # 过滤无效Query：空的、太短的、测试描述性的
                        if len(query) < 2:
                            continue
                        # 过滤以 Q+数字开头的测试描述（如"Q2落域到场景"）
                        if re.match(r'^Q\d+', query):
                            continue
                        # 过滤纯数字
                        if query.isdigit():
                            continue

                        case = TestCase(
                            id=f"{base_id}-{round_num}",
                            query=query,
                            expected_keywords=[expected] if expected else [],
                            category=f"{base_category}-{module}" if module else base_category,
                            remark=base_remark,
                        )
                        self._cases.extend(self._expand_rounds(case))
                        logger.debug(f"添加用例: {case.id} - {case.query}")
                        case_idx += 1
        else:
            # 标准格式
            col_map = self._map_columns(headers)
            logger.info(f"列映射结果: {col_map}")

            for i, row in enumerate(rows[1:], start=1):
                if not row or not any(row):
                    skipped += 1
                    continue
                logger.debug(f"处理第 {i} 行: {row}")
                case = self._row_to_case(i, row, headers, col_map)
                if case and case.query:
                    self._cases.extend(self._expand_rounds(case))
                    logger.debug(f"添加用例: {case.id} - {case.query}")
                else:
                    skipped += 1
                    logger.debug(f"跳过第 {i} 行 (无有效query)")

        self._source_path = path
        wb.close()
        logger.info(f"Excel 加载完成: 共 {len(self._cases)} 条用例, 跳过 {skipped} 行")
        return True, f"已加载 {len(self._cases)} 条用例", len(self._cases)

    @staticmethod
    def _detect_multi_round_format(headers: list[str]) -> bool:
        """检测是否为多轮测试格式"""
        query_count = sum(1 for h in headers if 'query' in h.lower())
        return query_count >= 2  # 如果有2个以上 query 列，认为是多轮格式

    def _load_csv(self, path: Path) -> tuple[bool, str, int]:
        """从 CSV 加载"""
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            return False, "CSV 文件为空", 0

        # 定位表头行：部分导出文件首行为汇总行（如"执行了 N 条用例..."）
        hdr_idx = self._find_header_row(rows)
        headers = [h.strip().lower() for h in rows[hdr_idx]]
        col_map = self._map_columns(headers)

        self._cases.clear()
        for i, row in enumerate(rows[hdr_idx + 1:], start=1):
            if not row or not any(row):
                continue
            case = self._row_to_case(i, row, headers, col_map)
            if case and case.query:
                self._cases.extend(self._expand_rounds(case))

        self._source_path = path
        return True, f"已加载 {len(self._cases)} 条用例", len(self._cases)

    @staticmethod
    def _find_header_row(rows: list) -> int:
        """定位表头行：前 5 行中包含强列名特征（query/步骤/期望/需求名称等）的行；
        找不到再放宽到"用例/语音"等弱特征；均无则返回 0。
        注: 导出文件首行常为汇总行（如"执行了 241 条用例，PASS: ..."），需跳过。"""
        strong = ("query", "步骤描述", "步骤", "指令", "期望结果", "需求名称", "测试点", "测试标题")
        weak = ("用例", "期望", "语音", "操作")
        for i, r in enumerate(rows[:5]):
            if not r:
                continue
            joined = "".join(str(x) for x in r[:12]).lower()
            if any(k in joined for k in strong):
                return i
        for i, r in enumerate(rows[:5]):
            if not r:
                continue
            joined = "".join(str(x) for x in r[:12]).lower()
            if any(k in joined for k in weak):
                return i
        return 0

    def _load_txt(self, path: Path) -> tuple[bool, str, int]:
        """从 TXT 加载 (每行一条 query)"""
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        self._cases.clear()
        for i, line in enumerate(lines, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 支持格式: query | expected_keywords | category
            parts = [p.strip() for p in line.split("|")]
            case = TestCase(
                id=f"TC{i:04d}",
                query=parts[0],
                expected_keywords=[k.strip() for k in parts[1].split(",")] if len(parts) > 1 and parts[1] else [],
                category=parts[2] if len(parts) > 2 else "",
            )
            self._cases.extend(self._expand_rounds(case))

        self._source_path = path
        return True, f"已加载 {len(self._cases)} 条用例", len(self._cases)

    def _load_json(self, path: Path) -> tuple[bool, str, int]:
        """从 JSON 加载"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and "cases" in data:
            items = data["cases"]
        else:
            return False, "JSON 格式不正确，需要数组或 {cases: []}", 0

        self._cases.clear()
        for i, item in enumerate(items, start=1):
            if isinstance(item, str):
                case = TestCase(id=f"TC{i:04d}", query=item)
            elif isinstance(item, dict):
                # expected_keywords 类型兼容：数组或逗号分隔字符串
                kw = item.get("expected_keywords", [])
                if isinstance(kw, str):
                    kw = [k.strip() for k in kw.split(",") if k.strip()]
                elif not isinstance(kw, list):
                    kw = []
                case = TestCase(
                    id=item.get("id", f"TC{i:04d}"),
                    query=item.get("query", ""),
                    expected_keywords=kw,
                    expected_intent=item.get("expected_intent", ""),
                    category=item.get("category", ""),
                    priority=item.get("priority", "P1"),
                    remark=item.get("remark", ""),
                )
            else:
                continue
            if case.query:
                self._cases.extend(self._expand_rounds(case))

        self._source_path = path
        return True, f"已加载 {len(self._cases)} 条用例", len(self._cases)

    @staticmethod
    def _map_columns(headers: list[str]) -> dict[str, int]:
        """映射列名到索引 (支持模糊匹配)"""
        logger.debug(f"开始列映射，表头: {headers}")
        col_map: dict[str, int] = {}
        # 别名列表：越靠前的别名优先级越高
        aliases = {
            "id":       ["id", "编号", "序号", "case_id", "用例id", "用例编号", "病例id", "病例ID", "用例ID", "用例编号", "需求id", "需求ID"],
            "query":    ["query", "步骤描述", "操作步骤", "测试步骤", "指令内容", "语音指令", "灌入文本", "测试指令", "步骤", "操作", "输入", "指令", "语音", "文本", "command", "text", "病例描述", "描述", "需求描述", "测试描述", "操作描述"],
            "expected": ["expected", "期望结果", "预期结果", "验证标准", "期望", "预期", "expected_keywords", "关键词", "检查点", "验证点", "期望输出"],
            "intent":   ["intent", "意图", "expected_intent", "期望意图", "意图类型"],
            "category": ["category", "需求名称", "分类", "标签", "tag", "模块", "功能模块", "功能", "场景", "需求"],
            "priority": ["priority", "优先级", "p", "重要程度"],
            "remark":   ["remark", "备注", "说明", "note", "comment", "前置条件", "前置"],
        }
        used_indices: set[int] = set()

        # 第一轮：精确匹配（Unicode 标准化比较）
        for key, alias_list in aliases.items():
            logger.debug(f"精确匹配检查 '{key}'，别名: {alias_list[:5]}...")
            matched = False
            for i, h in enumerate(headers):
                if i in used_indices:
                    continue
                h_norm = unicodedata.normalize('NFC', h.strip())
                for alias in alias_list:
                    alias_norm = unicodedata.normalize('NFC', alias.strip())
                    if h_norm == alias_norm:
                        col_map[key] = i
                        used_indices.add(i)
                        logger.debug(f"  精确匹配成功: '{key}' <- '{h}' -> 列 {i}")
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                logger.debug(f"  精确匹配未找到: {key}")

        # 第二轮：模糊匹配 (仅对未匹配的字段)
        for key, alias_list in aliases.items():
            if key in col_map:
                continue
            logger.debug(f"模糊匹配字段 '{key}'，候选别名: {alias_list}")
            best_match = -1
            best_score = 0
            for i, h in enumerate(headers):
                if i in used_indices:
                    continue
                for alias in alias_list:
                    score = 0
                    # 表头完全等于别名 (最高分)
                    if h == alias:
                        score = 200
                    # 表头包含别名 (别名越长越精确，分数越高)
                    elif alias in h:
                        # 基础分 + 别名长度平方 (鼓励更长的匹配)
                        score = 50 + len(alias) ** 2
                    # 别名包含表头 (低分，容易误匹配)
                    elif h in alias and len(h) >= 2:
                        score = 20 + len(h)

                    if score > best_score:
                        best_match = i
                        best_score = score
                        logger.debug(f"  候选: '{h}' <-> '{alias}' -> 列 {i}, 分数={score}")
            if best_match >= 0:
                col_map[key] = best_match
                used_indices.add(best_match)
                logger.debug(f"  模糊匹配结果: '{key}' -> 列 {best_match}, 分数={best_score}")
            else:
                logger.warning(f"  未找到匹配列: {key}")

        logger.info(f"列映射最终结果: {col_map}")
        return col_map

    def _row_to_case(self, idx: int, row: tuple | list, headers: list[str],
                     col_map: dict[str, int]) -> TestCase | None:
        """将行数据转为 TestCase"""
        logger.debug(f"解析第 {idx} 行: {row}")
        logger.debug(f"列映射: {col_map}")

        def get_val(key: str, default: str = "") -> str:
            if key in col_map:
                ci = col_map[key]
                if ci < len(row) and row[ci] is not None:
                    val = str(row[ci]).strip()
                    logger.debug(f"  取值 '{key}': 列{ci}={val}")
                    return val
                else:
                    logger.debug(f"  取值 '{key}': 列{ci} 越界或为空")
            else:
                logger.debug(f"  取值 '{key}': 无映射")
            return default

        query = get_val("query")
        if not query:
            # 如果没有明确的 query 列，尝试第一列
            if row and row[0]:
                query = str(row[0]).strip()
                logger.debug(f"  使用第一列作为 query: {query}")
            else:
                logger.warning(f"  第 {idx} 行无有效 query，跳过")
                return None

        expected_str = get_val("expected")
        expected_keywords = [k.strip() for k in expected_str.split(",") if k.strip()] if expected_str else []

        case = TestCase(
            id=get_val("id", f"TC{idx:04d}"),
            query=query,
            expected_keywords=expected_keywords,
            expected_intent=get_val("intent"),
            category=get_val("category"),
            priority=get_val("priority", "P1"),
            remark=get_val("remark"),
        )
        logger.info(f"生成用例: id={case.id}, query='{case.query}', "
                    f"keywords={case.expected_keywords}, intent='{case.expected_intent}', "
                    f"category='{case.category}'")
        return case

    @staticmethod
    def split_multi_round_query(query: str) -> list[tuple[str, str]]:
        """识别 query 内的 Q1:/Q2:/Q3: 多轮前缀并拆分。

        返回 [(轮次号, 子查询文本), ...]；无多轮标记时返回 [("", query)]。
        示例: "Q1:画小猫\\nQ2:加一只小狗" → [("1","画小猫"), ("2","加一只小狗")]
        """
        if not query:
            return [("", query)]
        matches = list(re.finditer(r'Q(\d+)\s*[：:]\s*([\s\S]*?)(?=Q\d+\s*[：:]|$)', query))
        # 拆分条件：出现 ≥2 个轮次，或整条以 Q 前缀开头（单轮带前缀）
        if len(matches) >= 2 or (len(matches) == 1 and matches[0].start() == 0):
            segs = []
            for m in matches:
                text = m.group(2).strip()
                if text:
                    segs.append((m.group(1), text))
            if segs:
                return segs
        return [("", query)]

    def _expand_rounds(self, case: TestCase) -> list[TestCase]:
        """按 Q 前缀将多轮 query 拆分为多条独立用例（继承 id/期望/分类等）。
        开关 split_multi_round 关闭时不做拆分。"""
        if not self._split_multi_round:
            return [case]
        parts = self.split_multi_round_query(case.query)
        if len(parts) == 1 and not parts[0][0]:
            return [case]
        out = []
        for round_no, text in parts:
            c = TestCase(
                id=f"{case.id}-Q{round_no}" if round_no else case.id,
                query=text,
                expected_keywords=list(case.expected_keywords),
                expected_intent=case.expected_intent,
                category=case.category,
                priority=case.priority,
                remark=case.remark,
            )
            out.append(c)
        return out

    def clear(self) -> None:
        self._cases.clear()
        self._source_path = None

    def reset_all(self) -> None:
        for case in self._cases:
            case.reset()

    def get_stats(self) -> dict[str, int]:
        """获取统计信息"""
        stats = {"total": len(self._cases), "pending": 0, "running": 0,
                 "pass": 0, "fail": 0, "skip": 0, "error": 0}
        for case in self._cases:
            if case.status in stats:
                stats[case.status] += 1
        return stats

    def export_results(self, output_path: str | Path) -> tuple[bool, str]:
        """导出测试结果到 Excel（⚠️ P2 待外迁到 core/report.py）"""
        try:
            import openpyxl
            from openpyxl.styles import Alignment, Font, PatternFill
        except ImportError:
            return False, "需要安装 openpyxl"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "测试结果"

        # 表头
        headers = ["用例ID", "语音指令", "期望关键词", "分类", "优先级",
                   "状态", "ASR识别", "意图类型", "ASR响应时间(s)", "系统响应时间(s)",
                   "实际结果", "错误信息", "备注"]
        ws.append(headers)

        # 表头样式
        header_fill = PatternFill(start_color="4F6BF6", end_color="4F6BF6", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True, size=11)
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # 状态颜色
        status_fills = {
            "pass": PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid"),
            "fail": PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
            "skip": PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
            "error": PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
        }

        for case in self._cases:
            row_data = [
                case.id, case.query,
                ", ".join(case.expected_keywords) if case.expected_keywords else "",
                case.category, case.priority,
                case.status.upper(), case.asr_text, case.intent_type,
                case.asr_response_time if case.asr_response_time > 0 else "",
                case.sys_response_time if case.sys_response_time > 0 else "",
                case.actual_result, case.error_msg, case.remark,
            ]
            ws.append(row_data)

            # 给状态列上色
            status_cell = ws.cell(row=ws.max_row, column=6)
            if case.status in status_fills:
                status_cell.fill = status_fills[case.status]

        # 调整列宽
        col_widths = [10, 30, 20, 12, 8, 8, 20, 15, 12, 12, 30, 20, 15]
        for i, width in enumerate(col_widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width

        # 添加统计 sheet
        ws2 = wb.create_sheet("统计")
        stats = self.get_stats()
        ws2.append(["指标", "数量"])
        ws2.append(["总用例数", stats["total"]])
        ws2.append(["通过", stats["pass"]])
        ws2.append(["失败", stats["fail"]])
        ws2.append(["跳过", stats["skip"]])
        ws2.append(["错误", stats["error"]])
        ws2.append(["待执行", stats["pending"]])
        if stats["total"] > 0:
            pass_rate = stats["pass"] / (stats["pass"] + stats["fail"]) * 100 if (stats["pass"] + stats["fail"]) > 0 else 0
            ws2.append(["通过率", f"{pass_rate:.1f}%"])

        for cell in ws2[1]:
            cell.font = Font(bold=True)

        wb.save(str(output_path))
        return True, f"报告已保存: {output_path}"
