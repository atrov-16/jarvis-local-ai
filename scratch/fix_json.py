import re

with open("jarvis/storage/repositories.py", "r") as f:
    code = f.read()

# Replace json.loads(str(data.pop("key_json", "default")))
# with json.loads(data.pop("key_json") or "default")
# Wait, if data.pop("key_json") is None, it becomes None or "default", which is "default".
# Then json.loads("default") works!
# Let's fix line 672 as well which is: json.loads(str(data.pop("metadata_json")))
# This should be: json.loads(data.pop("metadata_json") or "{}")

code = re.sub(
    r'json\.loads\(str\(data\.pop\("([^"]+)",\s*"([^"]+)"\)\)\)',
    r'json.loads(data.pop("\1") or "\2")',
    code
)

code = re.sub(
    r'json\.loads\(str\(data\.pop\("([^"]+)"\)\)\)',
    r'json.loads(data.pop("\1") or "{}")',
    code
)

with open("jarvis/storage/repositories.py", "w") as f:
    f.write(code)

print("Fixed JSON loads in repositories.py")
