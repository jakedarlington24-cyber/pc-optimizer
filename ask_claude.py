import anthropic

from pc_tools import (
    ACTIONS_NEEDING_RESTORE_POINT,
    ACTIONS_REQUIRING_CONFIRMATION,
    MAX_TOKENS,
    MODEL,
    SYSTEM_PROMPT,
    TOOLS,
    create_restore_point,
    describe_action,
    get_api_key,
    run_tool,
)


def handle_tool_call(block):
    if block.name not in ACTIONS_REQUIRING_CONFIRMATION:
        print(f"[checking: {block.name}]")
        result = run_tool(block.name, block.input)
        if block.name == "open_page":
            print(f"[{result}]")
        return result

    print(f"\n{describe_action(block.name, block.input)}")
    if input("Proceed? (y/n): ").strip().lower() != "y":
        return "User declined this action."

    note = ""
    if block.name in ACTIONS_NEEDING_RESTORE_POINT:
        print("[creating a System Restore point first, for safety...]")
        ok, message = create_restore_point()
        print(f"[{message}]")
        if not ok:
            note = " (Note: a restore point could not be created before this action.)"

    print("[running action...]")
    result = run_tool(block.name, block.input)
    print(f"[{result}]")
    return result + note


def run_conversation(client, conversation):
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=conversation,
        )
        conversation.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\nAssistant: {block.text}\n")

        if response.stop_reason != "tool_use":
            return

        tool_results = [
            {"type": "tool_result", "tool_use_id": block.id, "content": handle_tool_call(block)}
            for block in response.content
            if block.type == "tool_use"
        ]
        conversation.append({"role": "user", "content": tool_results})


def main():
    client = anthropic.Anthropic(api_key=get_api_key())
    print("PC Assistant — type your question, or 'quit' to exit.\n")
    conversation = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.lower() in ("quit", "exit"):
            break
        if not user_input:
            continue

        turn_start = len(conversation)
        conversation.append({"role": "user", "content": user_input})
        try:
            run_conversation(client, conversation)
        except anthropic.APIError as e:
            del conversation[turn_start:]
            print(f"\n[Error talking to Claude: {e}]\n")


if __name__ == "__main__":
    main()
