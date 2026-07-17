import asyncio
import websockets
import json
import struct
import win32file
import win32pipe
import pywintypes
import threading
import time

PIPE_NAME = r'\\.\pipe\TakionData'
# 104 bytes structure per tick
STRUCT_FORMAT = '<32sddddddQIiII'
STRUCT_SIZE = 104

footprints = {}
ws_clients = set()
ws_loop = None

def get_current_minute_ts():
    return int(time.time() // 60) * 60

async def ws_handler(websocket, path=None):
    ws_clients.add(websocket)
    try:
        # Send current state
        await websocket.send(json.dumps({"type": "init", "data": footprints}))
        async for msg in websocket:
            pass
    finally:
        ws_clients.remove(websocket)

def run_ws_server():
    global ws_loop
    ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_loop)
    async def main():
        async with websockets.serve(ws_handler, "localhost", 8766):
            await asyncio.Future()
    ws_loop.run_until_complete(main())

def broadcast_update(symbol, candle):
    if ws_clients and ws_loop is not None:
        try:
            msg = json.dumps({"type": "update", "symbol": symbol, "candle": candle})
            ws_loop.call_soon_threadsafe(websockets.broadcast, ws_clients, msg)
        except Exception:
            pass

def process_tick(symbol, open_p, high_p, low_p, last_p, bid, ask, cum_vol, time_ms, bid_size, ask_size, pos):
    if symbol not in footprints:
        footprints[symbol] = {
            "candles": [],
            "last_cum_vol": cum_vol,
        }
    
    symbol_data = footprints[symbol]
    last_vol = symbol_data["last_cum_vol"]
    
    trade_size = cum_vol - last_vol
    symbol_data["last_cum_vol"] = cum_vol
    
    current_ts = get_current_minute_ts()
    
    if not symbol_data["candles"] or symbol_data["candles"][-1]["timestamp"] != current_ts:
        # New candle
        new_candle = {
            "timestamp": current_ts,
            "open": last_p,
            "high": last_p,
            "low": last_p,
            "close": last_p,
            "footprint": {}
        }
        symbol_data["candles"].append(new_candle)
        # Keep last 60 candles
        if len(symbol_data["candles"]) > 60:
            symbol_data["candles"].pop(0)
    
    candle = symbol_data["candles"][-1]
    
    # Update OHLC
    candle["high"] = max(candle["high"], last_p)
    candle["low"] = min(candle["low"], last_p)
    candle["close"] = last_p
    
    # Process footprint trade
    if trade_size > 0:
        price_str = f"{last_p:.2f}"
        if price_str not in candle["footprint"]:
            candle["footprint"][price_str] = {"bid": 0, "ask": 0}
            
        if last_p >= ask:
            candle["footprint"][price_str]["ask"] += trade_size
        elif last_p <= bid:
            candle["footprint"][price_str]["bid"] += trade_size
        else:
            half = trade_size // 2
            candle["footprint"][price_str]["ask"] += half
            candle["footprint"][price_str]["bid"] += (trade_size - half)

    # Broadcast update
    broadcast_update(symbol, candle)

def read_pipe_loop():
    print("Starting TakionData Pipe Server...")
    while True:
        handle = None
        try:
            handle = win32pipe.CreateNamedPipe(
                PIPE_NAME, win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                255, 1024 * 1024, 1024 * 1024, 0, None
            )
            print("Pipe Created. Waiting for Takion to connect...")
            win32pipe.ConnectNamedPipe(handle, None)
            print("SUCCESS! Connected to Takion Data Pipe! Awaiting ticks...")
            
            buffer = b""
            
            while True:
                resp = win32file.ReadFile(handle, 1024 * 1024)
                buffer += resp[1]
                
                num_structs = len(buffer) // STRUCT_SIZE
                if num_structs > 0:
                    process_len = num_structs * STRUCT_SIZE
                    data_to_process = buffer[:process_len]
                    buffer = buffer[process_len:]
                    
                    chunks = list(struct.iter_unpack(STRUCT_FORMAT, data_to_process))
                    
                    for chunk in chunks:
                        b_sym, open_p, high_p, low_p, last_p, bid, ask, cum_vol, time_ms, pos, bid_size, ask_size = chunk
                        symbol = b_sym.split(b'\x00')[0].decode('utf-8', errors='ignore').strip().upper()
                        
                        if not symbol:
                            continue
                            
                        process_tick(symbol, open_p, high_p, low_p, last_p, bid, ask, cum_vol, time_ms, bid_size, ask_size, pos)
                        
        except Exception as e:
            print("Pipe error:", e)
            if handle:
                try: win32file.CloseHandle(handle)
                except: pass
            time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=run_ws_server, daemon=True).start()
    read_pipe_loop()