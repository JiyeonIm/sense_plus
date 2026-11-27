import ollama

response = ollama.chat(
    model="qwen3-vl:4b-instruct",
    messages=[
        {
            "role": "user",
            "content": "안녕? 너는 어떤 기능을 수행할 수 있어?"
        }
    ]
)

print(response.message.content)