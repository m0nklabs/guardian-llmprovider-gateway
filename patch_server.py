with open("/home/flip/llama_cpp_guardian/app/proxy/server.py", "r") as f:
    text = f.read()

import re

target = r'''        # Detect streaming requests for chat/completions — must proxy SSE in real-time
        is_stream = False
        if path == "chat/completions":
            try:
                json_body = json.loads(body)
                is_stream = json_body.get("stream", False)
            except (json.JSONDecodeError, Exception):
                pass'''

replacement = r'''        # Detect streaming requests for chat/completions — must proxy SSE in real-time
        is_stream = False
        if path == "chat/completions":
            try:
                json_body = json.loads(body)
                is_stream = json_body.get("stream", False)
                # WORKAROUND: llama.cpp "Assistant response prefill is incompatible with enable_thinking"
                msgs = json_body.get("messages", [])
                
                # Consolidate ALL trailing assistant messages
                trailing_assistant_contents = []
                while len(msgs) > 0 and msgs[-1].get("role") == "assistant":
                    popped = msgs.pop()
                    content = popped.get("content", "")
                    if content:
                        trailing_assistant_contents.insert(0, str(content))
                        
                if trailing_assistant_contents and len(msgs) >= 1:
                    combined_prefill = "\\n".join(trailing_assistant_contents)
                    
                    # Find the last user message and append the prefill instruction
                    last_user_idx = -1
                    for i in range(len(msgs)-1, -1, -1):
                        if msgs[i].get("role") == "user":
                            last_user_idx = i
                            break
                            
                    if last_user_idx != -1:
                        msgs[last_user_idx]["content"] = str(msgs[last_user_idx].get("content", "")) + f"\n\n[System directive: Please start your response exactly with the following text: {combined_prefill}]"
                        json_body["messages"] = msgs
                        body = json.dumps(json_body).encode("utf-8")
                    else:
                        import logging
                        logging.getLogger("uvicorn.error").warning("Found trailing assistant messages but no user message to attach to.")
            except (json.JSONDecodeError, Exception):
                pass'''

if target in text:
    print("Found target block exactly.")
    text = text.replace(target, replacement)
else:
    print("Target block not found precisely. Falling back to regex.")
    text = re.sub(r'is_stream = False\n        if path == "chat/completions":\n            try:\n                json_body = json\.loads\(body\)\n                is_stream = json_body\.get\("stream", False\)\n            except \(json\.JSONDecodeError, Exception\):\n                pass', replacement, text, flags=re.MULTILINE)

with open("/home/flip/llama_cpp_guardian/app/proxy/server.py", "w") as f:
    f.write(text)
