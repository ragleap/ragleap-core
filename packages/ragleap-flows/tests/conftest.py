import pytest
from ragleap_tools import Tool, ToolResult, Toolkit
from ragleap_security import Scope

import ragleap_flows as rf


class Calls:
    def __init__(self):
        self.log = []


@pytest.fixture
def calls():
    return Calls()


@pytest.fixture
def kit(calls):
    def echo(**kw):
        calls.log.append(("echo", kw))
        return ToolResult(success=True, result=kw)

    def send(**kw):
        calls.log.append(("send", kw))
        return ToolResult(success=True, result={"sent": True, "to": kw.get("to")})

    def boom(**kw):
        calls.log.append(("boom", kw))
        return ToolResult(success=False, error="nope")

    def big(**kw):
        return ToolResult(success=True, result="x" * 70_000)

    def fetch(**kw):
        calls.log.append(("fetch", kw))
        return ToolResult(success=True, result={"text": "external page"})

    any_obj = {"type": "object", "properties": {}}
    return Toolkit([
        Tool("echo", "echo", {"type": "object", "properties": {"value": {}, "to": {"type": "string"}}}, echo),
        Tool("send", "send", {"type": "object", "properties": {"to": {"type": "string"}, "body": {}}}, send),
        Tool("boom", "boom", any_obj, boom),
        Tool("big", "big", any_obj, big),
        Tool("fetch", "fetch", any_obj, fetch),
    ])


A, B = Scope("acme"), Scope("globex")


def make(steps, **extra):
    return rf.from_dict({"version": 1, "steps": steps, **extra})


def refund_flow():
    return make([
        {"id": "look", "type": "tool", "tool": "echo", "arguments": {"value": "$input.amount"}, "next": "big_or_small"},
        {"id": "big_or_small", "type": "branch",
         "cases": [{"when": {"path": "$look.value", "op": "gt", "value": 100}, "goto": "ask"}], "else": "pay"},
        {"id": "ask", "type": "approval", "prompt": "Approve this refund?", "show": {"amount": "$look.value"}, "next": "pay"},
        {"id": "pay", "type": "tool", "tool": "send", "arguments": {"to": "$input.who", "body": "$look.value"}},
    ], output={"paid": "$pay.sent"})
