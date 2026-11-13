"""递归下降解析器。

语法（优先级从低到高）与标准 SQL 布尔表达式一致：

    or_expr   := and_expr ( OR and_expr )*
    and_expr  := not_expr ( AND not_expr )*
    not_expr  := NOT not_expr | predicate
    predicate := atom ( ( '=' | '<>' | '<' | '<=' | '>' | '>=' ) atom
                       | IS [ NOT ] NULL
                       | [ NOT ] IN '(' list_element ( ',' list_element )* ')' )?
    atom      := INT | NULL | IDENT | '(' or_expr ')'
    list_element := INT | NULL

结合性：AND/OR 均左结合；NOT 为右结合的前缀运算符。
不支持的语法（算术、函数、子查询、BETWEEN、字符串等）在词法或语法阶段定位拒绝。
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import ParseError
from .lexer import Token, tokenize

COMPARE_OPS = {"=", "<>", "<", "<=", ">", ">="}
_BOOLEAN_KINDS = {"IsNull", "InList", "Compare", "NotExpr", "BinBool"}


class Parser:
    def __init__(self, tokens: list[Token], columns: set[str]):
        self.toks = tokens
        self.pos = 0
        self.columns = columns

    # -- 基础工具 -----------------------------------------------------------

    def peek(self, offset: int = 0) -> Token:
        idx = min(self.pos + offset, len(self.toks) - 1)
        return self.toks[idx]

    def next(self) -> Token:
        t = self.toks[self.pos]
        if self.pos < len(self.toks) - 1:
            self.pos += 1
        return t

    def expect(self, kind: str, what: str) -> Token:
        t = self.peek()
        if t.kind != kind:
            if t.kind == "EOF":
                raise ParseError(f"缺少 {what}", t.line, t.col)
            raise ParseError(f"此处应为 {what}，但遇到 {t.text!r}", t.line, t.col)
        return self.next()

    # -- 入口 ---------------------------------------------------------------

    def parse(self) -> ast.Expr:
        expr = self.or_expr()
        t = self.peek()
        if t.kind != "EOF":
            raise ParseError(f"意外的词法单元 {t.text!r}，谓词在表达式结束后仍有多余内容", t.line, t.col)
        return expr

    # -- 各优先级 -----------------------------------------------------------

    def or_expr(self) -> ast.Expr:
        left = self.and_expr()
        while self.peek().kind == "OR":
            op_tok = self.next()
            right = self.and_expr()
            left = ast.BinBool("OR", left, right, op_tok.line, op_tok.col, right.end_col)
        return left

    def and_expr(self) -> ast.Expr:
        left = self.not_expr()
        while self.peek().kind == "AND":
            op_tok = self.next()
            right = self.not_expr()
            left = ast.BinBool("AND", left, right, op_tok.line, op_tok.col, right.end_col)
        return left

    def not_expr(self) -> ast.Expr:
        if self.peek().kind == "NOT":
            op_tok = self.next()
            operand = self.not_expr()
            return ast.NotExpr(operand, op_tok.line, op_tok.col, operand.end_col)
        node = self.predicate()
        if node.kind not in _BOOLEAN_KINDS:
            t = self.peek()
            raise ParseError(
                "此处需要布尔表达式（比较、IS NULL、IN、AND/OR/NOT 组合），不能只写一个字面量或列引用",
                t.line, t.col,
            )
        return node  # type: ignore[return-value]

    def predicate(self) -> ast.Node:
        node = self.atom()
        t = self.peek()

        if t.kind in COMPARE_OPS:
            op_tok = self.next()
            right = self.atom()
            self._require_leaf(right, "比较操作数")
            self._require_leaf(node, "比较操作数")
            return ast.Compare(op_tok.kind, node, right,  # type: ignore[arg-type]
                               op_tok.line, op_tok.col, right.end_col)

        if t.kind == "IS":
            is_tok = self.next()
            negate = False
            if self.peek().kind == "NOT":
                self.next()
                negate = True
            null_tok = self.expect("NULL", "NULL（IS [NOT] NULL 语法）")
            self._require_leaf(node, "IS NULL 的操作数")
            return ast.IsNull(node, negate, is_tok.line, is_tok.col,  # type: ignore[arg-type]
                              null_tok.end_col)

        if t.kind == "NOT" and self.peek(1).kind == "IN":
            self.next()  # NOT
            in_tok = self.next()  # IN
            return self._parse_in(node, in_tok, negate=True)

        if t.kind == "IN":
            in_tok = self.next()
            return self._parse_in(node, in_tok, negate=False)

        return node

    def _parse_in(self, node: ast.Node, in_tok: Token, negate: bool) -> ast.InList:
        self._require_leaf(node, "IN 左侧操作数")
        self.expect("(", "'(' 开始 IN 列表")
        elements: list[ast.Leaf] = []
        # 空列表在 SQL 中合法（恒为 FALSE），但题目明确要求至少一个元素。
        if self.peek().kind == ")":
            t = self.peek()
            raise ParseError("IN 列表至少需要一个元素，且只能是整数或 NULL", t.line, t.col)
        while True:
            elem_tok = self.peek()
            if elem_tok.kind not in ("INT", "NULL"):
                if elem_tok.kind == "EOF":
                    raise ParseError("IN 列表未闭合，缺少 ')'", elem_tok.line, elem_tok.col)
                raise ParseError(
                    "IN 列表元素只能是整数或 NULL 字面量（不支持列引用、表达式或子查询）",
                    elem_tok.line, elem_tok.col,
                )
            elements.append(self.atom())  # type: ignore[arg-type]
            if self.peek().kind == ",":
                self.next()
                continue
            close = self.expect(")", "IN 列表结束的 ')'")
            break
        return ast.InList(node, tuple(elements), negate,  # type: ignore[arg-type]
                          in_tok.line, in_tok.col, close.end_col)

    def atom(self) -> ast.Node:
        t = self.peek()
        if t.kind == "INT":
            self.next()
            return ast.IntLit(t.value, t.line, t.col, t.end_col)
        if t.kind == "NULL":
            self.next()
            return ast.NullLit(t.line, t.col, t.end_col)
        if t.kind == "IDENT":
            self.next()
            if t.text not in self.columns:
                raise ParseError(f"未知列名 {t.text!r}（未在 columns 中声明，列名区分大小写）",
                                 t.line, t.col)
            return ast.Column(t.text, t.line, t.col, t.end_col)
        if t.kind == "(":
            self.next()
            inner = self.or_expr()
            self.expect(")", "右括号 ')'")
            return inner
        if t.kind == "EOF":
            raise ParseError("缺少操作数（表达式意外结束）", t.line, t.col)
        raise ParseError(f"此处应为列引用、整数、NULL 或 '('，但遇到 {t.text!r}",
                         t.line, t.col)

    def _require_leaf(self, node: ast.Node, role: str) -> None:
        if node.kind in _BOOLEAN_KINDS:
            raise ParseError(
                f"{role}只能是列引用、整数或 NULL，不能是带括号的布尔表达式",
                node.line, node.col,
            )


def parse_predicate(text: str, columns: set[str]) -> tuple[list[Token], ast.Expr]:
    """词法 + 语法分析，返回（去掉 EOF 的 token 列表，AST）。"""
    tokens = tokenize(text)
    parser = Parser(tokens, columns)
    tree = parser.parse()
    return tokens[:-1], tree
