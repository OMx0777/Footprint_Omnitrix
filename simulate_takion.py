import asyncio
import websockets
import json
import pandas as pd
import time
import argparse
import threading
import sys

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

def process_tick(symbol, price, size, side):
    if symbol not in footprints:
        footprints[symbol] = {
            "candles": [],
            "last_cum_vol": 0,
        }
    
    symbol_data = footprints[symbol]
    
    trade_size = size
    symbol_data["last_cum_vol"] += size
    
    current_ts = get_current_minute_ts()
    
    if not symbol_data["candles"] or symbol_data["candles"][-1]["timestamp"] != current_ts:
        # New candle
        new_candle = {
            "timestamp": current_ts,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "footprint": {}
        }
        symbol_data["candles"].append(new_candle)
        # Keep last 60 candles
        if len(symbol_data["candles"]) > 60:
            symbol_data["candles"].pop(0)
    
    candle = symbol_data["candles"][-1]
    
    # Update OHLC
    candle["high"] = max(candle["high"], price)
    candle["low"] = min(candle["low"], price)
    candle["close"] = price
    
    # Process footprint trade
    if trade_size > 0:
        price_str = f"{price:.2f}"
        if price_str not in candle["footprint"]:
            candle["footprint"][price_str] = {"bid": 0, "ask": 0}
            
        if side == 'A':
            candle["footprint"][price_str]["ask"] += trade_size
        elif side == 'B':
            candle["footprint"][price_str]["bid"] += trade_size
        else:
            half = trade_size // 2
            candle["footprint"][price_str]["ask"] += half
            candle["footprint"][price_str]["bid"] += (trade_size - half)

    # Broadcast update
    broadcast_update(symbol, candle)


def main():
    parser = argparse.ArgumentParser(description="Standalone Footprint WebSocket Simulator")
    parser.add_argument("--file", type=str, default=r"C:\Users\ADMIN\Desktop\5year_tick_by_tickdata\AAPL\2021.parquet", help="Path to parquet file")
    parser.add_argument("--speed", type=float, default=0.01, help="Delay in seconds between ticks")
    args = parser.parse_args()
    
    print("Starting Standalone WebSocket Server on port 8766...")
    threading.Thread(target=run_ws_server, daemon=True).start()
    
    # Give WS server a moment to start
    time.sleep(1)
    
    print(f"Loading {args.file}...")
    df = pd.read_parquet(args.file)
    print(f"Loaded {len(df)} rows. Filtering for trades...")
    
    trades = df[df['action'] == 'T'].copy()
    print(f"Found {len(trades)} trades.")
    
    print(f"Streaming data directly to WebSocket UI at speed {args.speed}s per tick...")
    print("WARNING: This completely bypasses TakionData pipes and footprint_server.py so your live automation is safe.")
    
    count = 0
    try:
        for idx, row in trades.iterrows():
            symbol = str(row['symbol']).upper()
            if not symbol or symbol == "NAN":
                symbol = "AAPL"
                
            price = float(row['price'])
            size = int(row['size'])
            side = str(row['side'])
            
            process_tick(symbol, price, size, side)
            
            count += 1
            if count % 100 == 0:
                print(f"Sent {count} ticks to UI (Current Price: {price})")
            
            time.sleep(args.speed)
            
    except KeyboardInterrupt:
        print("\nSimulation stopped.")

if __name__ == '__main__':
    main()