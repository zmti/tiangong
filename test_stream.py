import requests

r = requests.post(
    'http://127.0.0.1:8517/v1/chat/stream',
    json={'message': '1+1', 'conversation_id': 't11', 'mode': 'auto'},
    stream=True,
    timeout=90
)

count = 0
done_found = False
for line in r.iter_lines(decode_unicode=True):
    if line:
        count += 1
        if 'done' in line:
            print(f"{count}: {line[:150]}", flush=True)
            done_found = True
            break
        if 'error' in line:
            print(f"{count}: {line[:150]}", flush=True)
        if count > 500:
            print(f"... stopping at {count} (no done found)")
            break

print(f"Total events: {count}, done found: {done_found}")
