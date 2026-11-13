"""受限公式求值引擎。

职责：
- 展开共享公式：跟随格按主格位移平移相对引用，得到各自独立的 AST；
- 按依赖图递归求值，显式状态机定位循环（含跨表循环）；
- IF 只求值被选分支（未选分支中的除零不会发生）；
- IFERROR 只捕获公式错误（FormulaError），不吞工具自身缺陷；
- SUM 的范围聚合与标量算术使用不同的文本/布尔语义，严格区分；
- 不支持的特性落为带原因的错误值，独立单元格照常求值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import parser as P
from .errors import (
    CIRCULAR,
    NA,
    REF,
    UNSUPPORTED,
    VALUE,
    FormulaError,
)
from .ooxml import CellRecord, WorkbookData
from .values import CellValue, arithmetic, compare, negate

_UNSEEN, _EVALUATING, _DONE = 0, 1, 2


@dataclass
class CellResult:
    sheet: str
    coord: str
    value: CellValue
    is_formula: bool
    status: str  # ok / error / unsupported
    reason: str | None = None
    formula_text: str | None = None       # 原始公式文本（跟随格为主格原文）
    expanded_formula: str | None = None   # 跟随格位移展开后的公式
    dependencies: list[str] = field(default_factory=list)  # 实际求值过的依赖 Sheet!A1


class Engine:
    def __init__(self, wb: WorkbookData):
        self.wb = wb
        self._results: dict[tuple[str, int, int], CellResult] = {}
        self._state: dict[tuple[str, int, int], int] = {}
        self._stack: list[tuple[str, int, int]] = []
        # 每表共享公式主格索引：si -> (col, row)
        self._masters: dict[str, dict[str, tuple[int, int]]] = {}
        self.warnings: list[str] = []
        self._master_ast: dict[tuple[str, int, int], object] = {}
        self._index_masters()

    # ---------------- 共享公式主格索引 ----------------
    def _index_masters(self) -> None:
        for sheet_name, sheet in self.wb.sheets.items():
            index: dict[str, tuple[int, int]] = {}
            for (col, row), cell in sheet.cells.items():
                f = cell.formula
                if f is not None and f.kind == "shared_master":
                    assert f.si is not None
                    if f.si in index:
                        # 不合法的 OOXML：保留首个主格，登记警告而非整体崩溃
                        self.warnings.append(
                            f"工作表 {sheet_name} 共享公式索引 {f.si} 存在重复主格: "
                            f"{_coord_of(*index[f.si])} 与 {cell.coord}，已忽略后者"
                        )
                        continue
                    index[f.si] = (col, row)
            self._masters[sheet_name] = index

    # ---------------- 对外入口 ----------------
    def evaluate_all(self) -> dict[tuple[str, int, int], CellResult]:
        for sheet_name in self.wb.sheet_order:
            sheet = self.wb.sheets[sheet_name]
            for col, row in sorted(sheet.cells, key=lambda k: (k[1], k[0])):
                key = (sheet_name, col, row)
                if self._state.get(key) != _DONE:
                    self._evaluate_cell(sheet_name, col, row)
        return self._results

    def evaluate_cell(self, sheet_name: str, col: int, row: int) -> CellResult:
        return self._evaluate_cell(sheet_name, col, row)

    def results(self) -> dict[tuple[str, int, int], CellResult]:
        return self._results

    # ---------------- 单元格求值 ----------------
    def _evaluate_cell(self, sheet_name: str, col: int, row: int) -> CellResult:
        key = (sheet_name, col, row)
        state = self._state.get(key, _UNSEEN)
        if state == _DONE:
            return self._results[key]
        if state == _EVALUATING:
            chain = " -> ".join(self._ref_label(k) for k in self._stack) + " -> " + self._ref_label(key)
            raise FormulaError(CIRCULAR, f"检测到循环引用: {chain}")

        sheet = self.wb.sheets.get(sheet_name)
        cell_key = (col, row)
        # 先判断真实单元格；只有表/格确实不存在时才按空白处理，
        # 否则真实格会被提前缓存成空白占位
        if sheet is None or cell_key not in sheet.cells:
            result = CellResult(
                sheet=sheet_name, coord=_coord_of(col, row),
                value=CellValue.blank(), is_formula=False, status="ok",
            )
            self._state[key] = _DONE
            self._results[key] = result
            return result

        cell = sheet.cells[cell_key]
        self._state[key] = _EVALUATING
        self._stack.append(key)
        deps: list[str] = []
        try:
            if cell.formula is None:
                result = self._static_result(cell, sheet_name)
            else:
                result = self._formula_result(cell, sheet_name, deps)
        except FormulaError as exc:
            status = "unsupported" if exc.code == UNSUPPORTED else "error"
            result = CellResult(
                sheet=sheet_name, coord=cell.coord,
                value=CellValue.error(exc.code, exc.reason),
                is_formula=cell.formula is not None,
                status=status, reason=exc.reason,
                formula_text=self._original_text(cell, sheet_name),
                expanded_formula=self._safe_expanded_text(cell, sheet_name),
                dependencies=deps,
            )
        finally:
            self._stack.pop()
        self._state[key] = _DONE
        self._results[key] = result
        return result

    def _static_result(self, cell: CellRecord, sheet_name: str) -> CellResult:
        sv = cell.static
        if sv.kind == "blank":
            value = CellValue.blank()
        elif sv.kind == "number":
            value = CellValue.number(sv.value)  # type: ignore[arg-type]
        elif sv.kind == "boolean":
            value = CellValue.boolean(sv.value)  # type: ignore[arg-type]
        elif sv.kind == "text":
            # 注意：以 = 开头的普通文本保持文本，绝不作为公式执行
            value = CellValue.text(sv.value)  # type: ignore[arg-type]
        elif sv.kind == "error":
            value = CellValue.error(str(sv.value), sv.reason or "静态错误值")
        else:  # pragma: no cover - 穷尽 StaticValue 的 kind
            value = CellValue.error(NA, f"未知静态值类型 {sv.kind!r}")
        return CellResult(
            sheet=sheet_name, coord=cell.coord, value=value,
            is_formula=False, status="error" if value.is_error else "ok",
            reason=sv.reason,
        )

    # ---------------- 公式准备与求值 ----------------
    def _formula_result(
        self, cell: CellRecord, sheet_name: str, deps: list[str]
    ) -> CellResult:
        f = cell.formula
        assert f is not None
        original = self._original_text(cell, sheet_name)

        # 显式不支持的公式容器（数组 / 数据表 / 其他）
        if f.unsupported_reason is not None:
            raise FormulaError(UNSUPPORTED, f.unsupported_reason)

        ast, expanded = self._build_ast(cell, sheet_name)
        ctx = _EvalContext(self, sheet_name, deps)
        value = ctx.eval(ast)
        return CellResult(
            sheet=sheet_name, coord=cell.coord, value=value,
            is_formula=True,
            status="error" if value.is_error else "ok",
            reason=value.reason if value.is_error else None,
            formula_text=original, expanded_formula=expanded,
            dependencies=list(deps),
        )

    def _original_text(self, cell: CellRecord, sheet_name: str) -> str | None:
        f = cell.formula
        if f is None:
            return None
        if f.kind == "shared_follower":
            assert f.si is not None
            master = self._masters[sheet_name].get(f.si)
            if master is None:
                return None
            master_cell = self.wb.sheets[sheet_name].cells[master]
            return master_cell.formula.text  # type: ignore[union-attr]
        return f.text

    def _safe_expanded_text(self, cell: CellRecord, sheet_name: str) -> str | None:
        try:
            _, expanded = self._build_ast(cell, sheet_name)
            return expanded
        except FormulaError:
            return None

    def _build_ast(self, cell: CellRecord, sheet_name: str) -> tuple[object, str]:
        """返回（AST, 展开后公式文本）。普通公式展开文本即规范化原文。"""
        f = cell.formula
        assert f is not None
        if f.kind == "shared_follower":
            assert f.si is not None
            master_pos = self._masters[sheet_name].get(f.si)
            if master_pos is None:
                raise FormulaError(
                    UNSUPPORTED,
                    f"共享公式跟随格 {cell.coord} 找不到 si={f.si} 的主格",
                )
            master_cell = self.wb.sheets[sheet_name].cells[master_pos]
            master_ast = self._parse_master(sheet_name, master_pos, master_cell)
            dcol = cell.col - master_pos[0]
            drow = cell.row - master_pos[1]
            ast = P.shift(master_ast, dcol, drow)
            return ast, P.render(ast)
        # 普通公式 / 共享主格：主格位移为 (0,0)，直接解析
        if f.text is None:
            raise FormulaError(UNSUPPORTED, f"单元格 {cell.coord} 公式缺少文本")
        ast = P.parse_formula(f.text)
        return ast, P.render(ast)

    def _parse_master(self, sheet_name: str, pos: tuple[int, int], master_cell: CellRecord):
        if pos not in self._master_ast:
            text = master_cell.formula.text  # type: ignore[union-attr]
            self._master_ast[pos] = P.parse_formula(text)
        return self._master_ast[pos]

    # ---------------- 引用解析（供 _EvalContext 使用） ----------------
    def resolve_cell(self, sheet_name: str, col: int, row: int, deps: list[str]) -> CellValue:
        sheet = self.wb.sheets.get(sheet_name)
        if sheet is None:
            raise FormulaError(REF, f"引用了不存在的工作表 {sheet_name!r}")
        deps.append(f"{sheet_name}!{_coord_of(col, row)}")
        target = sheet.cells.get((col, row))
        if target is None:
            return CellValue.blank()
        return self._evaluate_cell(sheet_name, col, row).value

    def resolve_range(
        self, sheet_name: str, ref: P.Reference, deps: list[str]
    ) -> list[CellValue]:
        sheet = self.wb.sheets.get(sheet_name)
        if sheet is None:
            raise FormulaError(REF, f"引用了不存在的工作表 {sheet_name!r}")
        assert ref.is_range
        values: list[CellValue] = []
        for r in range(ref.row, ref.end_row + 1):  # type: ignore[arg-type]
            for c in range(ref.col, ref.end_col + 1):  # type: ignore[arg-type]
                deps.append(f"{sheet_name}!{_coord_of(c, r)}")
                target = sheet.cells.get((c, r))
                if target is None:
                    values.append(CellValue.blank())
                else:
                    values.append(self._evaluate_cell(sheet_name, c, r).value)
        return values

    @staticmethod
    def _ref_label(key: tuple[str, int, int]) -> str:
        sheet_name, col, row = key
        return f"{sheet_name}!{_coord_of(col, row)}"


def _coord_of(col: int, row: int) -> str:
    return f"{P.col_to_letters(col)}{row}"


# ---------------- AST 求值上下文 ----------------
class _EvalContext:
    def __init__(self, engine: Engine, current_sheet: str, deps: list[str]):
        self.engine = engine
        self.current_sheet = current_sheet
        self.deps = deps

    def eval(self, node: object) -> CellValue:
        if isinstance(node, P.Number):
            return CellValue.number(node.value)
        if isinstance(node, P.Text):
            return CellValue.text(node.value)
        if isinstance(node, P.Bool):
            return CellValue.boolean(node.value)
        if isinstance(node, P.Unsupported):
            raise FormulaError(UNSUPPORTED, node.reason)
        if isinstance(node, P.Unary):
            return negate(self.eval(node.operand))
        if isinstance(node, P.Binary):
            return self._binary(node)
        if isinstance(node, P.Reference):
            return self._reference(node)
        if isinstance(node, P.FunctionCall):
            return self._function(node)
        raise FormulaError(UNSUPPORTED, f"未知 AST 节点 {type(node).__name__}")

    def _binary(self, node: P.Binary) -> CellValue:
        if node.op in ("=", "<>", "<", "<=", ">", ">="):
            left = self._eval_comparison_operand(node.left)
            right = self._eval_comparison_operand(node.right)
            return compare(node.op, left, right)
        left = self.eval(node.left)
        right = self.eval(node.right)
        return arithmetic(node.op, left, right)

    def _eval_comparison_operand(self, node: object) -> CellValue:
        # 矩形范围不能直接参与比较
        if isinstance(node, P.Reference) and node.is_range:
            raise FormulaError(
                VALUE, f"矩形范围 {node.raw} 不能直接参与比较，需用聚合函数"
            )
        return self.eval(node)

    def _reference(self, ref: P.Reference) -> CellValue:
        sheet_name = ref.sheet or self.current_sheet
        if ref.is_range:
            raise FormulaError(
                VALUE, f"矩形范围 {ref.raw} 不能直接参与算术运算，需用 SUM 聚合"
            )
        return self.engine.resolve_cell(sheet_name, ref.col, ref.row, self.deps)

    def _function(self, node: P.FunctionCall) -> CellValue:
        if node.name == "SUM":
            return self._sum(node)
        if node.name == "IF":
            return self._if(node)
        return self._iferror(node)

    def _sum(self, node: P.FunctionCall) -> CellValue:
        total = 0.0
        for arg in node.args:
            if isinstance(arg, P.Reference):
                sheet_name = arg.sheet or self.current_sheet
                if arg.is_range:
                    values = self.engine.resolve_range(sheet_name, arg, self.deps)
                else:
                    values = [
                        self.engine.resolve_cell(sheet_name, arg.col, arg.row, self.deps)
                    ]
                # 范围/引用聚合语义：跳过空白、文本（含空串）、布尔；错误传播
                for v in values:
                    if v.type in ("blank", "empty_string", "text", "boolean"):
                        continue
                    if v.is_error:
                        raise FormulaError(v.value, v.reason or "SUM 引用到错误值")  # type: ignore[arg-type]
                    total += float(v.value)  # type: ignore[arg-type]
            else:
                # 标量表达式语义：布尔按 1/0，空白按 0，文本 #VALUE!
                v = self.eval(arg)
                total += v.to_scalar_number("SUM 标量参数")
        return CellValue.number(total)

    def _if(self, node: P.FunctionCall) -> CellValue:
        condition = self.eval(node.args[0])
        chosen = condition.to_logical("IF 条件")
        if chosen:
            return self.eval(node.args[1])
        if len(node.args) == 3:
            # 只求值被选分支：未选分支完全不触碰
            return self.eval(node.args[2])
        return CellValue.boolean(False)  # Excel 中缺省 else 返回 FALSE

    def _iferror(self, node: P.FunctionCall) -> CellValue:
        try:
            value = self.eval(node.args[0])
        except FormulaError:
            # 只捕获公式错误；其他 Python 异常属于工具缺陷，不得吞掉。
            # 错误来源格已在上面的 eval 中登记进 dependencies。
            return self.eval(node.args[1])
        if value.is_error:
            return self.eval(node.args[1])
        return value
