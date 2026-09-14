"""Execute the small IEC ST subset used by FB_TandemConveyor in software tests.

This parses the actual .TcPOU source, rather than duplicating the conveyor logic.
Unsupported syntax is an error. It is not a TwinCAT compiler or a hardware model.
TON uses the simulation clock; numeric assignments retain declared integer widths.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET


TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<typed>(?:UINT|UDINT|DINT|LINT|WORD)#(?:16#[0-9A-Fa-f]+|-?\d+))|"
    r"(?P<time>T\#\d+(?:MS|S))|"
    r"(?P<number>\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)|"
    r"(?P<name>[A-Za-z_]\w*)|"
    r"(?P<operator>:=|=>|<>|<=|>=|[+*/%=<>.,:;()\[\]-]))"
)


def tokenize(source):
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r"\(\*.*?\*\)", "", source, flags=re.S)
    result = []
    position = 0
    while source[position:].strip():
        match = TOKEN.match(source, position)
        if not match:
            raise SyntaxError(f"Unsupported ST at {source[position:position + 100]!r}")
        result.append(match.group(match.lastgroup))
        position = match.end()
    return result


def convert(value, kind):
    if kind == "BOOL":
        return bool(value)
    if kind in {"REAL", "LREAL"}:
        return float(value)
    if kind == "TIME":
        return int(value) / 1000.0
    widths = {"UINT": (16, False), "WORD": (16, False), "INT": (16, True),
              "UDINT": (32, False), "DINT": (32, True), "LINT": (64, True)}
    if kind not in widths:
        raise ValueError(f"Unsupported conversion {kind}")
    width, signed = widths[kind]
    result = round(value) if isinstance(value, float) else int(value)
    result %= 1 << width
    if signed and result >= (1 << (width - 1)):
        result -= 1 << width
    return result


def st_mod(a, b):
    quotient = abs(a) // abs(b)
    if (a < 0) != (b < 0):
        quotient = -quotient
    return a - quotient * b


class Ton:
    def __init__(self, simulation):
        self.simulation = simulation
        self.started = None
        self.Q = False

    def __call__(self, IN, PT):
        if not IN:
            self.started = None
            self.Q = False
        else:
            if self.started is None:
                self.started = self.simulation.time
            self.Q = self.simulation.time - self.started + 1e-12 >= PT


class Parser:
    PRECEDENCE = {"OR": 1, "AND": 2, "=": 3, "<>": 3, "<": 3,
                  ">": 3, "<=": 3, ">=": 3, "+": 4, "-": 4,
                  "*": 5, "/": 5, "MOD": 5}
    OPERATORS = {"OR": "|", "AND": "&", "=": "==", "<>": "!="}

    def __init__(self, source, variables):
        self.tokens = tokenize(source)
        self.cursor = 0
        self.variables = variables

    def peek(self, offset=0):
        index = self.cursor + offset
        return self.tokens[index] if index < len(self.tokens) else "EOF"

    def take(self, expected=None):
        value = self.peek()
        if expected is not None and value != expected:
            raise SyntaxError(f"Expected {expected}, got {value} near {self.tokens[self.cursor:self.cursor+12]}")
        self.cursor += 1
        return value

    def expression(self, minimum=0):
        token = self.take()
        if token in {"NOT", "-", "+"}:
            argument = self.expression(6)
            left = f"(not {argument})" if token == "NOT" else f"({token}{argument})"
        elif token == "(":
            left = self.expression()
            self.take(")")
        elif token in {"TRUE", "FALSE"}:
            left = "True" if token == "TRUE" else "False"
        elif token.startswith("T#"):
            factor = 0.001 if token.endswith("MS") else 1.0
            left = repr(float(re.search(r"\d+", token).group()) * factor)
        elif "#" in token:
            kind, literal = token.split("#", 1)
            number = int(literal[3:], 16) if literal.startswith("16#") else int(literal)
            left = str(convert(number, kind))
        elif token[0].isdigit():
            left = token
        elif self.peek() == "(":
            self.take("(")
            arguments = []
            while self.peek() != ")":
                arguments.append(self.expression())
                if self.peek() != ",":
                    break
                self.take(",")
            self.take(")")
            left = f"call({token!r}, {', '.join(arguments)})"
        elif token in self.variables:
            left = f"V[{token!r}]"
            if self.peek() == "[":
                self.take("[")
                index = self.expression()
                self.take("]")
                left += f"[{index}]"
            if self.peek() == ".":
                self.take(".")
                member = self.take()
                if member != "Q":
                    raise SyntaxError(f"Unsupported member {member}")
                left += ".Q"
        else:
            raise SyntaxError(f"Unknown ST token/variable {token}")
        while self.peek() in self.PRECEDENCE and self.PRECEDENCE[self.peek()] >= minimum:
            operator = self.take()
            right = self.expression(self.PRECEDENCE[operator] + 1)
            if operator == "MOD":
                left = f"st_mod({left}, {right})"
            else:
                left = f"({left} {self.OPERATORS.get(operator, operator)} {right})"
        return left

    def block(self, indent=0, endings=(), case_body=False):
        lines = []
        prefix = "    " * indent
        while self.peek() != "EOF" and self.peek() not in endings:
            if case_body and self.peek().isdigit() and self.peek(1) == ":":
                break
            keyword = self.take()
            if keyword == ";":
                continue
            if keyword == "IF":
                condition = self.expression()
                self.take("THEN")
                lines.append(prefix + f"if {condition}:")
                lines.extend(self.block(indent + 1, ("ELSIF", "ELSE", "END_IF")))
                while self.peek() == "ELSIF":
                    self.take()
                    condition = self.expression()
                    self.take("THEN")
                    lines.append(prefix + f"elif {condition}:")
                    lines.extend(self.block(indent + 1, ("ELSIF", "ELSE", "END_IF")))
                if self.peek() == "ELSE":
                    self.take()
                    lines.append(prefix + "else:")
                    lines.extend(self.block(indent + 1, ("END_IF",)))
                self.take("END_IF")
                self.take(";")
            elif keyword == "FOR":
                variable = self.take()
                self.take(":=")
                start = self.expression()
                self.take("TO")
                finish = self.expression()
                self.take("DO")
                lines.append(prefix + f"for V[{variable!r}] in range({start}, {finish} + 1):")
                lines.extend(self.block(indent + 1, ("END_FOR",)))
                self.take("END_FOR")
                self.take(";")
            elif keyword == "CASE":
                selector = self.expression()
                self.take("OF")
                first = True
                # Select once, as IEC CASE does even if an arm mutates its selector.
                lines.append(prefix + f"_case_value = {selector}")
                while self.peek() not in {"ELSE", "END_CASE"}:
                    arm = self.take()
                    if not arm.isdigit():
                        raise SyntaxError(f"Unsupported CASE arm {arm}")
                    self.take(":")
                    lines.append(prefix + f"{'if' if first else 'elif'} _case_value == {arm}:")
                    first = False
                    lines.extend(self.block(indent + 1, ("ELSE", "END_CASE"), True))
                if self.peek() == "ELSE":
                    self.take()
                    lines.append(prefix + "else:")
                    lines.extend(self.block(indent + 1, ("END_CASE",)))
                self.take("END_CASE")
                self.take(";")
            elif keyword in self.variables:
                if self.peek() == "(":
                    self.take("(")
                    arguments = []
                    while self.peek() != ")":
                        name = self.take()
                        self.take(":=")
                        arguments.append(f"{name}={self.expression()}")
                        if self.peek() != ",":
                            break
                        self.take(",")
                    self.take(")")
                    lines.append(prefix + f"V[{keyword!r}]({', '.join(arguments)})")
                else:
                    index = "None"
                    if self.peek() == "[":
                        self.take("[")
                        index = self.expression()
                        self.take("]")
                    self.take(":=")
                    value = self.expression()
                    lines.append(prefix + f"assign({keyword!r}, {index}, {value})")
                self.take(";")
            else:
                raise SyntaxError(f"Unsupported statement {keyword}")
        return lines or [prefix + "pass"]


class StSimulation:
    def __init__(self, tc_pou_path, cycle_seconds=0.001):
        root = ET.parse(tc_pou_path).getroot()
        declaration = re.sub(r"//[^\n]*", "", root.find(".//Declaration").text)
        self.time = 0.0
        self.dt = cycle_seconds
        self.values = {}
        self.types = {}
        pattern = re.compile(
            r"^\s*(\w+)\s*:\s*(?:ARRAY\[(\d+)\.\.(\d+)\]\s+OF\s+)?"
            r"(\w+)\s*(?::=\s*([^;]+))?;", re.M
        )
        for name, start, end, kind, default in pattern.findall(declaration):
            self.types[name] = kind
            if kind == "TON":
                self.values[name] = Ton(self)
            else:
                value = False if kind == "BOOL" else 0.0 if kind in {"REAL", "LREAL", "TIME"} else 0
                if default:
                    default_parser = Parser(default, {})
                    value = eval(default_parser.expression(), {"call": self.call, "st_mod": st_mod})
                self.values[name] = {i: value for i in range(int(start), int(end) + 1)} if start else value
        parser = Parser(root.find(".//ST").text, self.values)
        self.python_source = "\n".join(parser.block())
        if parser.peek() != "EOF":
            raise SyntaxError("Unparsed ST remains")
        self.code = compile(self.python_source, str(tc_pou_path) + " [ST simulation]", "exec")

    @staticmethod
    def call(name, *args):
        functions = {"ABS": abs, "MIN": min, "MAX": max, "SQRT": math.sqrt,
                     "LIMIT": lambda low, value, high: max(low, min(value, high))}
        if name in functions:
            return functions[name](*args)
        if "_TO_" in name:
            return convert(args[0], name.split("_TO_", 1)[1])
        raise ValueError(f"Unsupported ST function {name}")

    def assign(self, name, index, value):
        kind = self.types[name]
        # TIME variables already contain seconds after a T# literal or *_TO_TIME.
        result = value if kind == "TIME" else convert(value, kind)
        if index is None:
            self.values[name] = result
        else:
            self.values[name][index] = result

    def step(self, **inputs):
        for name, value in inputs.items():
            if name not in self.values:
                raise KeyError(name)
            self.assign(name, None, value)
        self.time += self.dt
        exec(self.code, {"V": self.values, "assign": self.assign,
                         "call": self.call, "st_mod": st_mod})
        return self.values
