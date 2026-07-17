const canvas = document.getElementById('footprint-canvas');
const ctx = canvas.getContext('2d');
const symbolSelect = document.getElementById('symbol-select');
const timeframeSelect = document.getElementById('timeframe-select');
const statusDiv = document.getElementById('status');
const themeToggle = document.getElementById('theme-toggle');
const modeBtns = document.querySelectorAll('.mode-btn');

let footprintData = {};
let currentSymbol = '';
let currentTimeframe = 1;
let currentMode = 'footprint';
let isSelectingAnchor = false;
let anchorTimestamp = null;

// Viewport state
let scaleX = 140; 
let scaleY = 2500; 
let offsetX = 0;
let offsetY = 0;
let isDraggingChart = false;
let isDraggingYAxis = false;
let isDraggingXAxis = false;
let lastDragX = 0;
let lastDragY = 0;

let mouseX = -1;
let mouseY = -1;
let mouseHover = false;

const AXIS_RIGHT = 75;
const AXIS_BOTTOM = 30;

let THEME = {
    bg: '#ffffff',
    grid: '#e0e3eb',
    text: '#131722',
    green: '#089981',
    red: '#f23645',
    blue: '#2962FF',
    volProfile: 'rgba(41, 98, 255, 0.15)',
    footprintBgGreen: 'rgba(8, 153, 129, 0.15)',
    footprintBgRed: 'rgba(242, 54, 69, 0.15)',
    textMuted: '#787b86',
    pocBg: '#131722',
    pocText: '#ffffff'
};

if (themeToggle) {
    themeToggle.addEventListener('click', () => {
        document.body.classList.toggle('dark-theme');
        if (document.body.classList.contains('dark-theme')) {
            THEME = {
                bg: '#131722', grid: '#2a2e39', text: '#d1d4dc',
                green: '#089981', red: '#f23645', blue: '#2962FF',
                volProfile: 'rgba(41, 98, 255, 0.15)',
                footprintBgGreen: 'rgba(8, 153, 129, 0.15)', footprintBgRed: 'rgba(242, 54, 69, 0.15)',
                textMuted: '#787b86', pocBg: '#d1d4dc', pocText: '#131722'
            };
        } else {
            THEME = {
                bg: '#ffffff', grid: '#e0e3eb', text: '#131722',
                green: '#089981', red: '#f23645', blue: '#2962FF',
                volProfile: 'rgba(41, 98, 255, 0.15)',
                footprintBgGreen: 'rgba(8, 153, 129, 0.15)', footprintBgRed: 'rgba(242, 54, 69, 0.15)',
                textMuted: '#787b86', pocBg: '#131722', pocText: '#ffffff'
            };
        }
        draw();
    });
}

modeBtns.forEach(btn => {
    btn.addEventListener('click', () => {
        modeBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentMode = btn.getAttribute('data-mode');
        
        if (currentMode === 'vp-anchored') {
            isSelectingAnchor = true;
            anchorTimestamp = null;
        } else {
            isSelectingAnchor = false;
        }
        draw();
    });
});

timeframeSelect.addEventListener('change', (e) => {
    currentTimeframe = parseInt(e.target.value);
    centerChart();
    draw();
});

function resizeCanvas() {
    canvas.width = canvas.parentElement.clientWidth;
    canvas.height = canvas.parentElement.clientHeight;
    draw();
}
window.addEventListener('resize', resizeCanvas);
resizeCanvas();

let ws = null;
function connectWS() {
    ws = new WebSocket('ws://localhost:8766');
    ws.onopen = () => {
        statusDiv.textContent = 'Connected';
        statusDiv.className = 'status connected';
    };
    ws.onclose = () => {
        statusDiv.textContent = 'Disconnected';
        statusDiv.className = 'status disconnected';
        setTimeout(connectWS, 3000);
    };
    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'init') {
            footprintData = msg.data;
            updateSymbolDropdown();
        } else if (msg.type === 'update') {
            if (!footprintData[msg.symbol]) {
                footprintData[msg.symbol] = { candles: [] };
                updateSymbolDropdown();
            }
            const symbolData = footprintData[msg.symbol];
            let found = false;
            for (let i = symbolData.candles.length - 1; i >= 0; i--) {
                if (symbolData.candles[i].timestamp === msg.candle.timestamp) {
                    symbolData.candles[i] = msg.candle;
                    found = true; break;
                }
            }
            if (!found) {
                symbolData.candles.push(msg.candle);
                if (symbolData.candles.length > 60) symbolData.candles.shift();
                
                if (msg.symbol === currentSymbol) {
                    const chartWidth = canvas.width - AXIS_RIGHT;
                    const maxX = (symbolData.candles.length * scaleX);
                    if (offsetX < chartWidth - maxX + scaleX * 2) {
                        offsetX = chartWidth - maxX - 100;
                    }
                }
            }
            if (msg.symbol === currentSymbol) draw();
        }
    };
}
connectWS();

function updateSymbolDropdown() {
    const symbols = Object.keys(footprintData).sort();
    if (symbols.length === 0) return;
    const currentValue = symbolSelect.value;
    symbolSelect.innerHTML = '';
    symbols.forEach(sym => {
        const opt = document.createElement('option');
        opt.value = sym; opt.textContent = sym;
        symbolSelect.appendChild(opt);
    });
    if (symbols.includes(currentValue)) {
        symbolSelect.value = currentValue;
    } else {
        symbolSelect.value = symbols[0];
        currentSymbol = symbols[0];
        centerChart();
    }
}

symbolSelect.addEventListener('change', (e) => {
    currentSymbol = e.target.value;
    centerChart();
});

function centerChart() {
    if (!currentSymbol || !footprintData[currentSymbol] || footprintData[currentSymbol].candles.length === 0) return;
    const candles = getDisplayCandles(footprintData[currentSymbol].candles, currentTimeframe);
    if (candles.length === 0) return;
    const lastCandle = candles[candles.length - 1];
    const chartHeight = canvas.height - AXIS_BOTTOM;
    const chartWidth = canvas.width - AXIS_RIGHT;
    offsetY = (chartHeight / 2) - (lastCandle.close * scaleY);
    offsetX = chartWidth - (candles.length * scaleX) - 100;
    draw();
}

canvas.addEventListener('mousedown', (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const chartWidth = canvas.width - AXIS_RIGHT;
    const chartHeight = canvas.height - AXIS_BOTTOM;
    
    // Anchor Selection Logic
    if (isSelectingAnchor && mx < chartWidth && my < chartHeight) {
        const timeIndex = Math.floor((mx - offsetX) / scaleX);
        const candles = getDisplayCandles(footprintData[currentSymbol].candles, currentTimeframe);
        if (timeIndex >= 0 && timeIndex < candles.length) {
            anchorTimestamp = candles[timeIndex].timestamp;
            isSelectingAnchor = false;
            draw();
        }
        return;
    }

    lastDragX = e.clientX;
    lastDragY = e.clientY;
    
    if (mx > chartWidth && my < chartHeight) isDraggingYAxis = true;
    else if (my > chartHeight && mx < chartWidth) isDraggingXAxis = true;
    else if (mx < chartWidth && my < chartHeight) isDraggingChart = true;
});

window.addEventListener('mouseup', () => {
    isDraggingChart = false;
    isDraggingYAxis = false;
    isDraggingXAxis = false;
    draw();
});

canvas.addEventListener('dblclick', () => {
    scaleY = 2500;
    scaleX = 140;
    centerChart();
});

window.addEventListener('mouseenter', () => { mouseHover = true; });
window.addEventListener('mouseleave', () => {
    mouseHover = false;
    isDraggingChart = false;
    isDraggingYAxis = false;
    isDraggingXAxis = false;
    draw();
});

window.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    mouseX = e.clientX - rect.left;
    mouseY = e.clientY - rect.top;
    const chartWidth = canvas.width - AXIS_RIGHT;
    const chartHeight = canvas.height - AXIS_BOTTOM;
    
    if (isDraggingChart) {
        offsetX += (e.clientX - lastDragX);
        offsetY += (e.clientY - lastDragY);
        lastDragX = e.clientX;
        lastDragY = e.clientY;
    } else if (isDraggingYAxis) {
        const dy = e.clientY - lastDragY;
        const zoomFactor = dy > 0 ? 0.95 : 1.05;
        const priceAtCenter = (chartHeight - (chartHeight/2) - offsetY) / scaleY;
        scaleY *= zoomFactor;
        offsetY = (chartHeight - (chartHeight/2)) - (priceAtCenter * scaleY);
        lastDragY = e.clientY;
    } else if (isDraggingXAxis) {
        const dx = e.clientX - lastDragX;
        const zoomFactor = dx > 0 ? 1.05 : 0.95;
        const timeAtCenter = ((chartWidth/2) - offsetX) / scaleX;
        scaleX = Math.max(20, scaleX * zoomFactor);
        offsetX = (chartWidth/2) - (timeAtCenter * scaleX);
        lastDragX = e.clientX;
    }
    
    if (isSelectingAnchor) canvas.style.cursor = 'crosshair';
    else if (mouseX > chartWidth && mouseY < chartHeight) canvas.style.cursor = 'ns-resize';
    else if (mouseY > chartHeight && mouseX < chartWidth) canvas.style.cursor = 'ew-resize';
    else canvas.style.cursor = 'crosshair';
    
    draw();
});

canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
    const chartWidth = canvas.width - AXIS_RIGHT;
    const chartHeight = canvas.height - AXIS_BOTTOM;
    
    if (mouseX > chartWidth) {
        const priceAtMouse = (chartHeight - mouseY - offsetY) / scaleY;
        scaleY *= zoomFactor;
        offsetY = (chartHeight - mouseY) - (priceAtMouse * scaleY);
    } 
    else if (mouseY > chartHeight) {
        const timeAtMouse = (mouseX - offsetX) / scaleX;
        scaleX = Math.max(20, scaleX * zoomFactor);
        offsetX = mouseX - (timeAtMouse * scaleX);
    }
    else {
        if (e.shiftKey) {
            const timeAtMouse = (mouseX - offsetX) / scaleX;
            scaleX = Math.max(20, scaleX * zoomFactor);
            offsetX = mouseX - (timeAtMouse * scaleX);
        } else {
            const priceAtMouse = (chartHeight - mouseY - offsetY) / scaleY;
            scaleY *= zoomFactor;
            offsetY = (chartHeight - mouseY) - (priceAtMouse * scaleY);
        }
    }
    draw();
});

function formatPrice(p) { return p.toFixed(2); }
function formatTime(ms) {
    const d = new Date(ms * 1000);
    return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
}
function formatVol(v) {
    if (v === 0) return "";
    if (v >= 1000) return (v / 1000).toFixed(2) + "K";
    return v.toString();
}

function getDisplayCandles(baseCandles, timeframeMinutes) {
    if (timeframeMinutes === 1 || baseCandles.length === 0) return baseCandles;
    const aggregated = [];
    let currentAgg = null;
    const intervalSec = timeframeMinutes * 60;
    for (let c of baseCandles) {
        const bucketTs = Math.floor(c.timestamp / intervalSec) * intervalSec;
        if (!currentAgg || currentAgg.timestamp !== bucketTs) {
            if (currentAgg) aggregated.push(currentAgg);
            currentAgg = { timestamp: bucketTs, open: c.open, high: c.high, low: c.low, close: c.close, footprint: {} };
        } else {
            currentAgg.high = Math.max(currentAgg.high, c.high);
            currentAgg.low = Math.min(currentAgg.low, c.low);
            currentAgg.close = c.close;
        }
        for (let price in c.footprint) {
            if (!currentAgg.footprint[price]) currentAgg.footprint[price] = { bid: 0, ask: 0 };
            currentAgg.footprint[price].bid += c.footprint[price].bid;
            currentAgg.footprint[price].ask += c.footprint[price].ask;
        }
    }
    if (currentAgg) aggregated.push(currentAgg);
    return aggregated;
}

// ----------------------------------------------------
// RENDERING FUNCTIONS
// ----------------------------------------------------

function drawGrid(chartWidth, chartHeight, minPrice, maxPrice, step, candles) {
    ctx.strokeStyle = THEME.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    const startPrice = Math.floor(minPrice / step) * step;
    for (let p = startPrice; p <= maxPrice; p += step) {
        const py = chartHeight - (p * scaleY + offsetY);
        if (py >= 0 && py <= chartHeight) {
            ctx.moveTo(0, Math.round(py) + 0.5);
            ctx.lineTo(chartWidth, Math.round(py) + 0.5);
        }
    }
    const skip = Math.max(1, Math.floor(120 / scaleX));
    if (candles) {
        ctx.setLineDash([2, 4]);
        for (let i = 0; i < candles.length; i += skip) {
            const x = offsetX + (i * scaleX) + scaleX/2;
            if (x >= 0 && x <= chartWidth) {
                ctx.moveTo(Math.round(x) + 0.5, 0);
                ctx.lineTo(Math.round(x) + 0.5, chartHeight);
            }
        }
        ctx.setLineDash([]);
    }
    ctx.stroke();
}

function drawAxes(chartWidth, chartHeight, minPrice, maxPrice, step, candles) {
    ctx.fillStyle = THEME.bg;
    ctx.fillRect(chartWidth, 0, AXIS_RIGHT, canvas.height); 
    ctx.fillRect(0, chartHeight, canvas.width, AXIS_BOTTOM);
    
    ctx.strokeStyle = THEME.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(chartWidth + 0.5, 0); ctx.lineTo(chartWidth + 0.5, canvas.height);
    ctx.moveTo(0, chartHeight + 0.5); ctx.lineTo(canvas.width, chartHeight + 0.5);
    ctx.stroke();
    
    const startPrice = Math.floor(minPrice / step) * step;
    ctx.fillStyle = THEME.text;
    ctx.font = '12px -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    
    for (let p = startPrice; p <= maxPrice; p += step) {
        const py = chartHeight - (p * scaleY + offsetY);
        if (py >= 0 && py <= chartHeight) {
            ctx.fillText(formatPrice(p), chartWidth + 6, py);
        }
    }
    
    if (candles && candles.length > 0) {
        ctx.textAlign = 'center';
        const skip = Math.max(1, Math.floor(120 / scaleX));
        for (let i = 0; i < candles.length; i += skip) {
            const x = offsetX + (i * scaleX) + scaleX/2;
            if (x >= 0 && x <= chartWidth) {
                const label = formatTime(candles[i].timestamp);
                ctx.fillText(label, x, chartHeight + AXIS_BOTTOM/2);
            }
        }
    }
}

function drawCandlestick(candle, x, chartHeight) {
    const openY = chartHeight - (candle.open * scaleY + offsetY);
    const closeY = chartHeight - (candle.close * scaleY + offsetY);
    const highY = chartHeight - (candle.high * scaleY + offsetY);
    const lowY = chartHeight - (candle.low * scaleY + offsetY);
    
    const candleWidth = Math.min(10, scaleX * 0.15);
    const candleCenterX = x + (scaleX * 0.1);
    
    const isBullish = candle.close >= candle.open;
    const color = isBullish ? THEME.green : THEME.red;
    
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(Math.round(candleCenterX) + 0.5, Math.round(highY));
    ctx.lineTo(Math.round(candleCenterX) + 0.5, Math.round(lowY));
    ctx.stroke();
    
    ctx.fillStyle = color;
    const bodyTop = Math.min(openY, closeY);
    const bodyHeight = Math.max(1, Math.abs(closeY - openY));
    ctx.fillRect(Math.round(candleCenterX - candleWidth/2), Math.round(bodyTop), candleWidth, bodyHeight);
}

function drawFootprintData(ctx, candle, x, chartHeight, tickSize, maxVol) {
    const fpKeys = Object.keys(candle.footprint).sort((a,b) => parseFloat(b) - parseFloat(a));
    const candleWidth = Math.min(10, scaleX * 0.15);
    const candleCenterX = x + (scaleX * 0.1);
    const footprintStartX = candleCenterX + (candleWidth / 2) + 4;
    const boxWidth = (scaleX * 0.8 - candleWidth - 8) / 2;
    const boxHeight = scaleY * tickSize;
    
    let pocPrice = null;
    let pocVol = -1;
    let totalVolBar = 0;
    let totalDelta = 0;
    
    fpKeys.forEach(priceStr => {
        const vols = candle.footprint[priceStr];
        const totalVol = vols.bid + vols.ask;
        totalVolBar += totalVol;
        totalDelta += (vols.ask - vols.bid);
        if (totalVol > pocVol) {
            pocVol = totalVol;
            pocPrice = parseFloat(priceStr);
        }
    });

    const imbalances = { bid: {}, ask: {} };
    fpKeys.forEach(priceStr => {
        const price = parseFloat(priceStr);
        const pUp = (price + tickSize).toFixed(2);
        const pDown = (price - tickSize).toFixed(2);
        
        if (candle.footprint[pDown]) {
            const bidDown = candle.footprint[pDown].bid;
            const askHere = candle.footprint[priceStr].ask;
            if (askHere > 0 && bidDown >= 0) {
                if (bidDown === 0) { if (askHere > 5) imbalances.ask[priceStr] = true; } 
                else if (askHere / bidDown > 3) imbalances.ask[priceStr] = true;
            }
        }
        if (candle.footprint[pUp]) {
            const askUp = candle.footprint[pUp].ask;
            const bidHere = candle.footprint[priceStr].bid;
            if (bidHere > 0 && askUp >= 0) {
                if (askUp === 0) { if (bidHere > 5) imbalances.bid[priceStr] = true; } 
                else if (bidHere / askUp > 3) imbalances.bid[priceStr] = true;
            }
        }
    });

    const fontSize = Math.max(8, Math.min(12, boxHeight * 0.7));
    ctx.font = `${fontSize}px -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    fpKeys.forEach(priceStr => {
        const price = parseFloat(priceStr);
        const vols = candle.footprint[priceStr];
        const pY = chartHeight - (price * scaleY + offsetY) - boxHeight/2;
        if (pY + boxHeight < 0 || pY > chartHeight) return;
        const isPoc = (price === pocPrice);
        
        // BID
        if (isPoc) ctx.fillStyle = THEME.pocBg;
        else if (imbalances.bid[priceStr]) ctx.fillStyle = THEME.red;
        else if (vols.bid > 0) ctx.fillStyle = THEME.footprintBgRed;
        else ctx.fillStyle = 'transparent';
        
        if (vols.bid > 0 || isPoc) {
            ctx.fillRect(footprintStartX, pY, boxWidth, boxHeight);
            ctx.fillStyle = (isPoc || imbalances.bid[priceStr]) ? THEME.pocText : THEME.text;
            const bidStr = formatVol(vols.bid);
            if (bidStr) ctx.fillText(bidStr, footprintStartX + boxWidth/2, pY + boxHeight/2);
        }
        
        // ASK
        if (isPoc) ctx.fillStyle = THEME.pocBg;
        else if (imbalances.ask[priceStr]) ctx.fillStyle = THEME.green;
        else if (vols.ask > 0) ctx.fillStyle = THEME.footprintBgGreen;
        else ctx.fillStyle = 'transparent';
        
        if (vols.ask > 0 || isPoc) {
            ctx.fillRect(footprintStartX + boxWidth, pY, boxWidth, boxHeight);
            ctx.fillStyle = (isPoc || imbalances.ask[priceStr]) ? THEME.pocText : THEME.text;
            const askStr = formatVol(vols.ask);
            if (askStr) ctx.fillText(askStr, footprintStartX + boxWidth + boxWidth/2, pY + boxHeight/2);
        }
        
        ctx.fillStyle = 'rgba(0,0,0,0.05)';
        ctx.fillRect(footprintStartX + boxWidth - 1, pY, 2, boxHeight);
        
        if (isPoc) {
            ctx.strokeStyle = THEME.textMuted;
            ctx.lineWidth = 1;
            ctx.strokeRect(footprintStartX, pY, boxWidth * 2, boxHeight);
        }
    });

    if (scaleX > 60 && fpKeys.length > 0) {
        const lowestPrice = parseFloat(fpKeys[fpKeys.length - 1]);
        const pY = chartHeight - (lowestPrice * scaleY + offsetY) - boxHeight/2;
        const bottomY = pY + boxHeight + 15;
        if (bottomY < chartHeight - 40 && bottomY > 0) {
            ctx.fillStyle = THEME.text;
            ctx.font = '11px -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif';
            ctx.textAlign = 'right';
            ctx.fillText("Delta", x + scaleX/2 - 4, bottomY);
            ctx.fillText("Total", x + scaleX/2 - 4, bottomY + 14);
            ctx.textAlign = 'left';
            ctx.fillStyle = totalDelta >= 0 ? THEME.green : THEME.red;
            ctx.fillText((totalDelta > 0 ? "+" : "") + formatVol(totalDelta), x + scaleX/2 + 4, bottomY);
            ctx.fillStyle = THEME.text;
            ctx.font = 'bold 11px -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif';
            ctx.fillText(formatVol(totalVolBar), x + scaleX/2 + 4, bottomY + 14);
        }
    }
}

function drawClusterData(ctx, candle, x, chartHeight, tickSize, maxVol) {
    const candleWidth = Math.min(10, scaleX * 0.15);
    const candleCenterX = x + (scaleX * 0.1);
    const boxHeight = scaleY * tickSize;
    
    Object.keys(candle.footprint).forEach(priceStr => {
        const price = parseFloat(priceStr);
        const vols = candle.footprint[priceStr];
        const pY = chartHeight - (price * scaleY + offsetY);
        if (pY + boxHeight < 0 || pY - boxHeight > chartHeight) return;
        
        const total = vols.bid + vols.ask;
        if (total === 0) return;
        const delta = vols.ask - vols.bid;
        
        const maxRadius = Math.max(2, (scaleX * 0.6) / 2);
        const radius = Math.max(2, Math.min(maxRadius, (total / (maxVol)) * maxRadius));
        
        ctx.beginPath();
        ctx.arc(candleCenterX + (scaleX * 0.5), pY, radius, 0, 2 * Math.PI);
        if (delta > 0) {
            ctx.fillStyle = THEME.footprintBgGreen;
            ctx.strokeStyle = THEME.green;
        } else if (delta < 0) {
            ctx.fillStyle = THEME.footprintBgRed;
            ctx.strokeStyle = THEME.red;
        } else {
            ctx.fillStyle = THEME.textMuted;
            ctx.strokeStyle = THEME.textMuted;
        }
        ctx.fill();
        ctx.lineWidth = 1;
        ctx.stroke();
    });
}

function drawHeatmap(ctx, chartWidth, chartHeight, candles, tickSize) {
    let absoluteMaxVol = 1;
    candles.forEach(c => {
        Object.values(c.footprint).forEach(v => {
            const tot = v.bid + v.ask;
            if (tot > absoluteMaxVol) absoluteMaxVol = tot;
        });
    });
    
    const getHeatColor = (val, max) => {
        const ratio = Math.min(1, val / max);
        if (ratio < 0.05) return 'transparent';
        if (ratio < 0.25) return `rgba(0, 0, ${100 + ratio*4*155}, 0.5)`;
        if (ratio < 0.5) return `rgba(${ (ratio-0.25)*4*255 }, 0, ${255 - (ratio-0.25)*4*255}, 0.7)`;
        if (ratio < 0.75) return `rgba(255, ${ (ratio-0.5)*4*255 }, 0, 0.8)`;
        return `rgba(255, 255, ${ (ratio-0.75)*4*255 }, 1.0)`;
    };

    const boxHeight = scaleY * tickSize;
    for (let i = 0; i < candles.length; i++) {
        const candle = candles[i];
        const x = offsetX + (i * scaleX);
        if (x + scaleX < 0 || x > chartWidth) continue;
        
        Object.keys(candle.footprint).forEach(priceStr => {
            const price = parseFloat(priceStr);
            const vols = candle.footprint[priceStr];
            const pY = chartHeight - (price * scaleY + offsetY) - boxHeight/2;
            if (pY + boxHeight < 0 || pY > chartHeight) return;
            const total = vols.bid + vols.ask;
            if (total === 0) return;
            
            ctx.fillStyle = getHeatColor(total, absoluteMaxVol);
            ctx.fillRect(x, Math.floor(pY), scaleX, Math.ceil(boxHeight) + 1);
        });
    }
}

function drawVolumeProfile(ctx, chartWidth, chartHeight, candles, tickSize, isAnchored, anchorTs) {
    const vp = {};
    let maxVpVol = 0;
    let totalVPVol = 0;
    
    candles.forEach(c => {
        if (isAnchored && anchorTs && c.timestamp < anchorTs) return;
        Object.keys(c.footprint).forEach(priceStr => {
            if (!vp[priceStr]) vp[priceStr] = { bid: 0, ask: 0, total: 0 };
            const v = c.footprint[priceStr];
            vp[priceStr].bid += v.bid;
            vp[priceStr].ask += v.ask;
            vp[priceStr].total += (v.bid + v.ask);
            totalVPVol += (v.bid + v.ask);
            if (vp[priceStr].total > maxVpVol) maxVpVol = vp[priceStr].total;
        });
    });
    
    if (totalVPVol === 0) return;
    
    const vpWidth = chartWidth * 0.25; 
    const boxHeight = scaleY * tickSize;
    
    let pocPrice = null;
    let pocVol = 0;
    Object.keys(vp).forEach(p => {
        if (vp[p].total > pocVol) {
            pocVol = vp[p].total;
            pocPrice = p;
        }
    });

    Object.keys(vp).forEach(priceStr => {
        const price = parseFloat(priceStr);
        const pY = chartHeight - (price * scaleY + offsetY) - boxHeight/2;
        if (pY + boxHeight < 0 || pY > chartHeight) return;
        
        const data = vp[priceStr];
        const barW = (data.total / maxVpVol) * vpWidth;
        const bidW = (data.bid / data.total) * barW;
        const askW = (data.ask / data.total) * barW;
        
        const startX = chartWidth - barW;
        
        ctx.globalAlpha = 0.6;
        ctx.fillStyle = THEME.red;
        ctx.fillRect(startX, pY, bidW, boxHeight);
        ctx.fillStyle = THEME.green;
        ctx.fillRect(startX + bidW, pY, askW, boxHeight);
        
        if (priceStr === pocPrice) {
            ctx.globalAlpha = 1.0;
            ctx.strokeStyle = THEME.pocBg;
            ctx.lineWidth = 1;
            ctx.strokeRect(startX, pY, barW, boxHeight);
        }
    });
    ctx.globalAlpha = 1.0;
    
    if (isAnchored && anchorTs) {
        const cIdx = candles.findIndex(c => c.timestamp === anchorTs);
        if (cIdx !== -1) {
            const ax = offsetX + (cIdx * scaleX) + scaleX/2;
            ctx.setLineDash([5, 5]);
            ctx.strokeStyle = THEME.highlight;
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(ax, 0); ctx.lineTo(ax, chartHeight);
            ctx.stroke();
            ctx.setLineDash([]);
        }
    }
}

function drawDeltaHistogram(ctx, chartWidth, chartHeight, candles) {
    const deltaZeroY = chartHeight - 20;
    let maxDelta = 1;
    const deltas = candles.map(c => {
        let cd = 0;
        Object.values(c.footprint).forEach(fp => cd += (fp.ask - fp.bid));
        if (Math.abs(cd) > maxDelta) maxDelta = Math.abs(cd);
        return cd;
    });
    
    ctx.strokeStyle = THEME.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, deltaZeroY + 0.5); ctx.lineTo(chartWidth, deltaZeroY + 0.5);
    ctx.stroke();
    
    for (let i = 0; i < candles.length; i++) {
        const x = offsetX + (i * scaleX);
        if (x + scaleX < 0 || x > chartWidth) continue;
        const cd = deltas[i];
        const barH = (Math.abs(cd) / maxDelta) * 20;
        ctx.fillStyle = cd >= 0 ? THEME.green : THEME.red;
        ctx.globalAlpha = 0.4;
        if (cd >= 0) ctx.fillRect(x + (scaleX*0.1), deltaZeroY - barH, scaleX*0.8, barH);
        else ctx.fillRect(x + (scaleX*0.1), deltaZeroY, scaleX*0.8, barH);
        ctx.globalAlpha = 1.0;
    }
}

function drawCrosshair(chartWidth, chartHeight, candles) {
    if (mouseHover && mouseX >= 0 && mouseX <= chartWidth && mouseY >= 0 && mouseY <= chartHeight && !isDraggingChart && !isDraggingYAxis && !isDraggingXAxis) {
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = THEME.textMuted;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(mouseX + 0.5, 0); ctx.lineTo(mouseX + 0.5, chartHeight);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(0, mouseY + 0.5); ctx.lineTo(chartWidth, mouseY + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        
        const priceAtCursor = (chartHeight - mouseY - offsetY) / scaleY;
        ctx.fillStyle = THEME.pocBg;
        ctx.fillRect(chartWidth, mouseY - 10, AXIS_RIGHT, 21);
        ctx.fillStyle = THEME.pocText;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(formatPrice(priceAtCursor), chartWidth + 6, mouseY);
        
        const timeIndex = Math.floor((mouseX - offsetX) / scaleX);
        let timeLabel = '-';
        if (timeIndex >= 0 && timeIndex < candles.length) timeLabel = formatTime(candles[timeIndex].timestamp);
        const lblWidth = ctx.measureText(timeLabel).width + 16;
        ctx.fillStyle = THEME.pocBg;
        ctx.fillRect(mouseX - lblWidth/2, chartHeight, lblWidth, AXIS_BOTTOM);
        ctx.fillStyle = THEME.pocText;
        ctx.textAlign = 'center';
        ctx.fillText(timeLabel, mouseX, chartHeight + AXIS_BOTTOM/2);
    }
}

// ----------------------------------------------------
// MAIN DRAW LOOP
// ----------------------------------------------------

function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const chartWidth = canvas.width - AXIS_RIGHT;
    const chartHeight = canvas.height - AXIS_BOTTOM;
    const minPrice = -offsetY / scaleY;
    const maxPrice = (chartHeight - offsetY) / scaleY;
    const priceDiff = maxPrice - minPrice;
    
    let step = 0.01;
    if (priceDiff > 10) step = 1.0;
    else if (priceDiff > 5) step = 0.5;
    else if (priceDiff > 1) step = 0.1;
    else if (priceDiff > 0.5) step = 0.05;
    
    const candles = footprintData[currentSymbol] ? getDisplayCandles(footprintData[currentSymbol].candles, currentTimeframe) : [];
    
    drawGrid(chartWidth, chartHeight, minPrice, maxPrice, step, candles);
    
    if (candles.length === 0) {
        drawAxes(chartWidth, chartHeight, minPrice, maxPrice, step, null);
        return;
    }
    
    const tickSize = 0.01;
    let maxVol = 1;
    let lastTradedPrice = 0;
    candles.forEach(c => {
        lastTradedPrice = c.close;
        Object.values(c.footprint).forEach(fp => {
            if (fp.bid > maxVol) maxVol = fp.bid;
            if (fp.ask > maxVol) maxVol = fp.ask;
            // For clusters we need max total volume per node
            if (fp.bid + fp.ask > maxVol) maxVol = fp.bid + fp.ask;
        });
    });

    if (currentMode === 'heatmap') drawHeatmap(ctx, chartWidth, chartHeight, candles, tickSize);
    
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, chartWidth, chartHeight);
    ctx.clip();
    
    for (let i = 0; i < candles.length; i++) {
        const candle = candles[i];
        const x = offsetX + (i * scaleX);
        if (x + scaleX < 0 || x > chartWidth) continue;
        
        drawCandlestick(candle, x, chartHeight);
        
        if (currentMode === 'footprint') {
            drawFootprintData(ctx, candle, x, chartHeight, tickSize, maxVol);
        } else if (currentMode === 'cluster') {
            drawClusterData(ctx, candle, x, chartHeight, tickSize, maxVol);
        }
    }
    
    if (currentMode === 'vp-fixed') {
        drawVolumeProfile(ctx, chartWidth, chartHeight, candles, tickSize, false, null);
    } else if (currentMode === 'vp-anchored') {
        drawVolumeProfile(ctx, chartWidth, chartHeight, candles, tickSize, true, anchorTimestamp);
    }
    
    drawDeltaHistogram(ctx, chartWidth, chartHeight, candles);
    ctx.restore();
    
    drawAxes(chartWidth, chartHeight, minPrice, maxPrice, step, candles);
    
    // Draw LTP
    if (lastTradedPrice > 0) {
        const ltpY = chartHeight - (lastTradedPrice * scaleY + offsetY);
        if (ltpY >= 0 && ltpY <= chartHeight) {
            ctx.setLineDash([2, 2]);
            ctx.strokeStyle = THEME.red;
            ctx.beginPath();
            ctx.moveTo(0, Math.round(ltpY) + 0.5); ctx.lineTo(chartWidth, Math.round(ltpY) + 0.5);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = THEME.red;
            ctx.fillRect(chartWidth, ltpY - 10, AXIS_RIGHT, 21);
            ctx.fillStyle = '#FFF';
            ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
            ctx.fillText(formatPrice(lastTradedPrice), chartWidth + 6, ltpY);
        }
    }
    
    drawCrosshair(chartWidth, chartHeight, candles);
}
