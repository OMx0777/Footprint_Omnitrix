import asyncio
import websockets

async def hello():
    try:
        async with websockets.connect('ws://localhost:8766') as ws:
            print('connected')
            msg = await ws.recv()
            print('received', msg)
    except Exception as e:
        print('error', e)

asyncio.run(hello())
