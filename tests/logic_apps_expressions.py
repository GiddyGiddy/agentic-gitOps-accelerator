"""Small evaluator for the Logic Apps expression subset used by the controller workflow.

It exists so tests can exercise the workflow's decision logic as written. It is
not a Logic Apps runtime: service semantics must still be verified in Azure.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_TOKEN = re.compile(r"\s*(?:('(?:[^']|'')*')|(\d+)|([A-Za-z_]\w*)|([(),\[\]?]))")


class ExpressionError(Exception):
    pass


def _tokens(text):
    position, result = 0, []
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match:
            raise ExpressionError(f"Cannot tokenize near {text[position:position + 20]!r}")
        position = match.end()
        string, number, name, punctuation = match.groups()
        if string is not None:
            result.append(("str", string[1:-1].replace("''", "'")))
        elif number is not None:
            result.append(("num", int(number)))
        elif name is not None:
            result.append(("name", name))
        else:
            result.append(("punct", punctuation))
    return result


class _Parser:
    def __init__(self, text):
        self.tokens, self.index = _tokens(text), 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else (None, None)

    def take(self, expected=None):
        token = self.peek()
        if token[0] is None or (expected and token[1] != expected):
            raise ExpressionError(f"Expected {expected!r}, found {token!r}")
        self.index += 1
        return token

    def parse(self):
        node = self.expression()
        if self.index != len(self.tokens):
            raise ExpressionError("Unexpected trailing tokens")
        return node

    def expression(self):
        kind, value = self.take()
        if kind in ("str", "num"):
            node = ("lit", value)
        elif kind == "name" and self.peek() == ("punct", "("):
            self.take("(")
            args = []
            if self.peek() != ("punct", ")"):
                args.append(self.expression())
                while self.peek() == ("punct", ","):
                    self.take(",")
                    args.append(self.expression())
            self.take(")")
            node = ("call", value, args)
        elif kind == "name" and value in ("true", "false", "null"):
            node = ("lit", {"true": True, "false": False, "null": None}[value])
        else:
            raise ExpressionError(f"Unexpected token {value!r}")
        while True:
            if self.peek() == ("punct", "?"):
                self.take("?")
                self.take("[")
                key = self.expression()
                self.take("]")
                node = ("index", node, key, True)
            elif self.peek() == ("punct", "["):
                self.take("[")
                key = self.expression()
                self.take("]")
                node = ("index", node, key, False)
            else:
                return node


def _timestamp(value):
    if not isinstance(value, str):
        raise ExpressionError(f"Not a timestamp: {value!r}")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z", value)
    if not match:
        raise ExpressionError(f"Not a UTC timestamp: {value!r}")
    fraction = (match.group(2) or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(match.group(1)).replace(microsecond=int(fraction), tzinfo=timezone.utc)


def _boolean(value):
    if not isinstance(value, bool):
        raise ExpressionError(f"Expected boolean, found {value!r}")
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExpressionError(f"Expected number, found {value!r}")
    return value


def _convert_time_zone(value, source, target, fmt):
    if (source, target, fmt) != ("UTC", "W. Europe Standard Time", "yyyy-MM"):
        raise ExpressionError("Unsupported time zone conversion")
    return _timestamp(value).astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m")


class Evaluator:
    def __init__(self, outputs=None, bodies=None, actions=None, parameters=None):
        self.outputs = outputs or {}
        self.bodies = bodies or {}
        self.actions = actions or {}
        self.parameters = parameters or {}
        self.item = None
        self.clock = None

    def evaluate(self, expression):
        if not isinstance(expression, str) or not expression.startswith("@") or expression.startswith("@{"):
            return expression
        return self._eval(_Parser(expression[1:]).parse())

    def _eval(self, node):
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "index":
            base, key = self._eval(node[1]), self._eval(node[2])
            if base is None:
                if node[3]:
                    return None
                raise ExpressionError("Cannot index null")
            if isinstance(base, dict):
                if key in base:
                    return base[key]
                if node[3]:
                    return None
                raise ExpressionError(f"Missing property {key!r}")
            raise ExpressionError(f"Cannot index {type(base).__name__}")
        name, args = node[1], node[2]
        if name == "if":
            condition = _boolean(self._eval(args[0]))
            return self._eval(args[1] if condition else args[2])
        values = [self._eval(arg) for arg in args]
        return self._call(name, values)

    def _call(self, name, v):
        if name == "equals":
            return v[0] == v[1] and type(v[0]) is type(v[1])
        if name == "and":
            return all(_boolean(x) for x in v)
        if name == "or":
            return any(_boolean(x) for x in v)
        if name == "not":
            return not _boolean(v[0])
        if name == "greater":
            return _number(v[0]) > _number(v[1])
        if name == "greaterOrEquals":
            return _number(v[0]) >= _number(v[1])
        if name == "less":
            return _number(v[0]) < _number(v[1])
        if name == "add":
            return _number(v[0]) + _number(v[1])
        if name == "sub":
            return _number(v[0]) - _number(v[1])
        if name == "mul":
            return _number(v[0]) * _number(v[1])
        if name == "div":
            return _number(v[0]) // _number(v[1])
        if name == "max":
            return max(_number(x) for x in v)
        if name == "length":
            if not isinstance(v[0], (list, str, dict)):
                raise ExpressionError("length requires a collection")
            return len(v[0])
        if name == "empty":
            return v[0] is None or len(v[0]) == 0
        if name == "coalesce":
            return next((x for x in v if x is not None), None)
        if name == "contains":
            if not isinstance(v[0], (list, str, dict)):
                raise ExpressionError("contains requires a collection")
            return v[1] in v[0]
        if name == "createArray":
            return list(v)
        if name == "first":
            return v[0][0] if v[0] else None
        if name == "concat":
            return "".join(str(x) for x in v)
        if name == "startsWith":
            return isinstance(v[0], str) and v[0].startswith(v[1])
        if name == "json":
            return json.loads(v[0])
        if name == "string":
            return v[0] if isinstance(v[0], str) else json.dumps(v[0], separators=(",", ":"))
        if name == "setProperty":
            return {**v[0], v[1]: v[2]}
        if name == "ticks":
            return int((_timestamp(v[0]) - datetime(1, 1, 1, tzinfo=timezone.utc)) / timedelta(microseconds=1)) * 10
        if name == "utcNow":
            return self.clock
        if name == "convertTimeZone":
            return _convert_time_zone(*v)
        if name == "addToTime":
            if v[2] != "Minute":
                raise ExpressionError("Unsupported time unit")
            return (_timestamp(v[0]) + timedelta(minutes=v[1])).strftime("%Y-%m-%dT%H:%M:%SZ")
        if name == "item":
            return self.item
        if name == "outputs":
            return self.outputs[v[0]]
        if name == "body":
            return self.bodies[v[0]]
        if name == "actions":
            return self.actions[v[0]]
        if name == "parameters":
            return self.parameters[v[0]]
        raise ExpressionError(f"Unsupported function {name}")

    def condition(self, expression):
        """Evaluate the JSON condition forms used by If actions."""
        if isinstance(expression, str):
            return _boolean(self.evaluate(expression))
        (operator, operands), = expression.items()
        if operator == "and":
            return all(self.condition(x) for x in operands)
        if operator == "not":
            return not self.condition(operands)
        left, right = (self.evaluate(x) for x in operands)
        if operator == "equals":
            return left == right
        if operator == "contains":
            return right in left
        raise ExpressionError(f"Unsupported condition {operator}")
