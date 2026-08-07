import sys
import re

with open("C:/Users/ADMIN/Desktop/Footprint_Omnitrix/tests/stress_1000.py", "r") as f:
    code = f.read()

# Add profiling attribution
old_model_mb = """    return lad/1e6, bs/1e6, tp/1e6, bars/1e6"""
new_model_mb = """    profiles = bbyts = caches = 0
    for ser in win.series.values():
        bbyts += len(ser._bar_by_ts) * 48  # rough dict entry overhead
        for bar in ser.bars:
            if bar._cache:
                caches += 280
            if bar._agg:
                caches += 200
                
    for p in win._panes[:win._n_panes]:
        if hasattr(p, 'profile') and p.profile:
            profiles += 10000 
            
    return lad/1e6, bs/1e6, tp/1e6, bars/1e6, profiles/1e6, bbyts/1e6, caches/1e6"""

code = code.replace(old_model_mb, new_model_mb)

# Update print statements
code = code.replace(
    'print("   elapsed    _tick ms          paint ms           RSS MB   backlog   "\\n      "ladders  buy/sell   tape    bars  (MB)")',
    'print("   elapsed    _tick ms          paint ms           RSS MB   backlog   "\\n      "ladders  buy/sell   tape    bars    prof   bbyts  caches  (MB)")'
)

code = code.replace(
    'lad, bsd, tpd, bard = model_mb()',
    'lad, bsd, tpd, bard, profd, bbytsd, cached = model_mb()'
)

code = code.replace(
    'model_series.append(lad + bsd + tpd + bard)',
    'model_series.append(lad + bsd + tpd + bard + profd + bbytsd + cached)'
)

code = code.replace(
    'print(f"   {el:5.0f}s   {tm:5.1f} (p95{tp:6.1f})  {pm:6.1f} (p95{pp:6.1f})  "\\n              f"{r:8.1f}   {max(backlogs):5d}   {lad:7.1f} {bsd:8.1f} {tpd:7.1f} "\\n              f"{bard:7.1f}")',
    'print(f"   {el:5.0f}s   {tm:5.1f} (p95{tp:6.1f})  {pm:6.1f} (p95{pp:6.1f})  "\\n              f"{r:8.1f}   {max(backlogs):5d}   {lad:7.1f} {bsd:8.1f} {tpd:7.1f} "\\n              f"{bard:7.1f} {profd:7.1f} {bbytsd:7.1f} {cached:7.1f}")'
)

code = code.replace('RUN_S = float(os.environ.get("STRESS_SECONDS", "300"))', 'RUN_S = float(os.environ.get("STRESS_SECONDS", "450"))')

with open("C:/Users/ADMIN/Desktop/Footprint_Omnitrix/tests/stress_1000.py", "w") as f:
    f.write(code)

print("Patched stress_1000.py")
