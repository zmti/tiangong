import sys, time, asyncio
sys.stdout.reconfigure(encoding='utf-8')

async def test():
    from app.core.agent import Agent
    
    agent = Agent(
        system_prompt='You are a helpful assistant.',
        temperature=0.7
    )
    
    print('Running agent test...')
    t0 = time.time()
    async for event in agent.run_stream('list files in current directory', history=[]):
        elapsed = time.time() - t0
        etype = event.get('type', '?')
        if etype == 'token':
            c = event.get('content', '')[:60]
            print(f'  [{elapsed:.1f}s] token: {c}', flush=True)
        elif etype == 'tool_call':
            print(f'  [{elapsed:.1f}s] TOOL: {event.get("name", "")}', flush=True)
        elif etype == 'tool_route':
            print(f'  [{elapsed:.1f}s] ROUTE: {event.get("tools", [])}', flush=True)
        elif etype == 'thinking':
            print(f'  [{elapsed:.1f}s] thinking', flush=True)
        elif etype == 'done':
            print(f'  [{elapsed:.1f}s] DONE model={event.get("model", "")}', flush=True)
            break
        elif elapsed > 45:
            print(f'  [{elapsed:.1f}s] TIMEOUT', flush=True)
            break
        else:
            print(f'  [{elapsed:.1f}s] {etype}', flush=True)

asyncio.run(test())
