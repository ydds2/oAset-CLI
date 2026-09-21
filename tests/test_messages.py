from oaset.agent.messages import Conversation, Message, ToolCallReq


def test_assistant_with_tool_calls_wire():
    msg = Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCallReq(id="c1", name="read_file", arguments='{"path":"a.txt"}')],
    )
    wire = msg.to_wire()
    assert wire["role"] == "assistant"
    assert "content" not in wire or wire["content"] is None
    assert wire["tool_calls"][0]["function"]["name"] == "read_file"
    assert wire["tool_calls"][0]["type"] == "function"


def test_tool_message_wire():
    msg = Message(role="tool", tool_call_id="c1", content="hi")
    wire = msg.to_wire()
    assert wire == {"role": "tool", "tool_call_id": "c1", "content": "hi"}


def test_roundtrip_persistence():
    msg = Message(
        role="assistant",
        content="text",
        tool_calls=[ToolCallReq("c9", "run_shell", "echo")],
        reasoning="thinking...",
        partial=True,
    )
    restored = Message.from_dict(msg.to_dict())
    assert restored.tool_calls[0].id == "c9"
    assert restored.reasoning == "thinking..."
    assert restored.partial is True


def test_conversation_wire_includes_system():
    conv = Conversation(system_prompt="sys")
    conv.append(Message(role="user", content="hello"))
    wire = conv.wire_messages()
    assert wire[0] == {"role": "system", "content": "sys"}
    assert wire[1]["content"] == "hello"


def test_token_estimate_counts_tools():
    conv = Conversation(system_prompt="abcd")
    conv.append(Message(role="user", content="abcd"))
    conv.append(
        Message(
            role="assistant",
            tool_calls=[ToolCallReq("c", "run_shell", "abcdefgh")],
        )
    )
    assert conv.token_estimate() >= 4
