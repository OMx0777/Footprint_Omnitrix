const canvas = document.getElementById('footprint-canvas');
const ctx = canvas.getContext('2d');
const symbolSelect = document.getElementById('symbol-select');
const timeframeSelect = document.getElementById('timeframe-select');
const statusDiv = document.getElementById('status');

let footprintData = {};
let currentSymbol = '';
let currentTimeframe = 1;

timeframeSelect.addEventListener('change', (e) => {
    currentTimeframe = parseInt(e.target.value);
    centerChart();
    draw();
});


// Viewport state
let scaleX = 140; // pixels per candle width
let scaleY = 2500; // pixels per dollar (e.g. 0.01 tick = 25px height)
let offsetX = 0;
let offsetY = 0;
let isDragging = false;
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
                    found = true;
                    break;
                }
            }
            if (!found) {
                symbolData.candles.push(msg.candle);
                if (symbolData.candles.length > 60) {
                    symbolData.candles.shift();
                }
                
                // Auto pan X if viewing latest
                if (msg.symbol === currentSymbol) {
                    const chartWidth = canvas.width - AXIS_RIGHT;
                    const maxX = (symbolData.candles.length * scaleX);
                    if (offsetX < chartWidth - maxX + scaleX * 2) {
                        offsetX = chartWidth - maxX - 100;
                    }
                }
            }
            
            if (msg.symbol === currentSymbol) {
                draw();
            }
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
        opt.value = sym;
        opt.textContent = sym;
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
    isDragging = true;
    lastDragX = e.clientX;
    lastDragY = e.clientY;
});
window.addEventListener('mouseup', () => {
    isDragging = false;
    draw();
});
window.addEventListener('mouseenter', () => {
    mouseHover = true;
});
window.addEventListener('mouseleave', () => {
    mouseHover = false;
    isDragging = false;
    draw();
});

window.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    mouseX = e.clientX - rect.left;
    mouseY = e.clientY - rect.top;
    
    if (isDragging) {
        offsetX += (e.clientX - lastDragX);
        offsetY += (e.clientY - lastDragY);
        lastDragX = e.clientX;
        lastDragY = e.clientY;
    }
    
    draw();
});

canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
    
    const chartWidth = canvas.width - AXIS_RIGHT;
    const chartHeight = canvas.height - AXIS_BOTTOM;
    
    if (mouseX > chartWidth) {
        // Zoom Y axis only
        const priceAtMouse = (chartHeight - mouseY - offsetY) / scaleY;
        scaleY *= zoomFactor;
        offsetY = (chartHeight - mouseY) - (priceAtMouse * scaleY);
    } 
    else if (mouseY > chartHeight) {
        // Zoom X axis only
        const timeAtMouse = (mouseX - offsetX) / scaleX;
        scaleX = Math.max(40, scaleX * zoomFactor);
        offsetX = mouseX - (timeAtMouse * scaleX);
    }
    else {
        // Zoom both or based on shift
        if (e.shiftKey) {
            const timeAtMouse = (mouseX - offsetX) / scaleX;
            scaleX = Math.max(40, scaleX * zoomFactor);
            offsetX = mouseX - (timeAtMouse * scaleX);
        } else {
            const priceAtMouse = (chartHeight - mouseY - offsetY) / scaleY;
            scaleY *= zoomFactor;
            offsetY = (chartHeight - mouseY) - (priceAtMouse * scaleY);
        }
    }
    draw();
});

function formatPrice(p) {
    return p.toFixed(2);
}

function formatTime(ms) {
    const d = new Date(ms * 1000);
    return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
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
            currentAgg = {
                timestamp: bucketTs,
                open: c.open,
                high: c.high,
                low: c.low,
                close: c.close,
                footprint: {}
            };
        } else {
            currentAgg.high = Math.max(currentAgg.high, c.high);
            currentAgg.low = Math.min(currentAgg.low, c.low);
            currentAgg.close = c.close;
        }
        
        for (let price in c.footprint) {
            if (!currentAgg.footprint[price]) {
                currentAgg.footprint[price] = { bid: 0, ask: 0 };
            }
            currentAgg.footprint[price].bid += c.footprint[price].bid;
            currentAgg.footprint[price].ask += c.footprint[price].ask;
        }
    }
    if (currentAgg) aggregated.push(currentAgg);
    return aggregated;
}

function drawAxes(chartWidth, chartHeight, minPrice, maxPrice, step, candles) {
    // Fill axis backgrounds
    ctx.fillStyle = THEME.bg;
    ctx.fillRect(chartWidth, 0, AXIS_RIGHT, canvas.height); // Right Axis
    ctx.fillRect(0, chartHeight, canvas.width, AXIS_BOTTOM); // Bottom Axis
    
    // Axis borders
    ctx.strokeStyle = THEME.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(chartWidth + 0.5, 0);
    ctx.lineTo(chartWidth + 0.5, canvas.height);
    ctx.stroke();
    
    ctx.beginPath();
    ctx.moveTo(0, chartHeight + 0.5);
    ctx.lineTo(canvas.width, chartHeight + 0.5);
    ctx.stroke();
    
    // Y-Axis Labels
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
    
    // X-Axis Labels
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

function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    
    const chartWidth = canvas.width - AXIS_RIGHT;
    const chartHeight = canvas.height - AXIS_BOTTOM;
    
    // Grid Setup
    const minPrice = -offsetY / scaleY;
    const maxPrice = (chartHeight - offsetY) / scaleY;
    const priceDiff = maxPrice - minPrice;
    
    let step = 0.01;
    if (priceDiff > 10) step = 1.0;
    else if (priceDiff > 5) step = 0.5;
    else if (priceDiff > 1) step = 0.1;
    else if (priceDiff > 0.5) step = 0.05;
    
    // Draw background grid
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
    // Vertical grid lines (time)
    if (footprintData[currentSymbol]) {
        const candles = getDisplayCandles(footprintData[currentSymbol].candles, currentTimeframe);
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

    if (!currentSymbol || !footprintData[currentSymbol]) {
        drawAxes(chartWidth, chartHeight, minPrice, maxPrice, step, null);
        return;
    }
    
    const candles = getDisplayCandles(footprintData[currentSymbol].candles, currentTimeframe);
    let maxVol = 1;
    let lastTradedPrice = 0;
    candles.forEach(c => {
        lastTradedPrice = c.close;
        Object.values(c.footprint).forEach(fp => {
            if (fp.bid > maxVol) maxVol = fp.bid;
            if (fp.ask > maxVol) maxVol = fp.ask;
        });
    });

    const tickSize = 0.01;
    
    // Helper to format volume as K (e.g. 1.57 K)
    const formatVol = (v) => {
        if (v === 0) return "";
        if (v >= 1000) return (v / 1000).toFixed(2) + "K";
        return v.toString();
    };
    
    // Draw Chart Area
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, chartWidth, chartHeight);
    ctx.clip();
    
    for (let i = 0; i < candles.length; i++) {
        const candle = candles[i];
        
        // TradingView centers the entire footprint block on the timestamp x. 
        // We will allocate left space for the candlestick, then the two bid/ask columns.
        const x = offsetX + (i * scaleX);
        
        if (x + scaleX < 0 || x > chartWidth) continue;
        
        const openY = chartHeight - (candle.open * scaleY + offsetY);
        const closeY = chartHeight - (candle.close * scaleY + offsetY);
        const highY = chartHeight - (candle.high * scaleY + offsetY);
        const lowY = chartHeight - (candle.low * scaleY + offsetY);
        
        // Define widths
        const candleWidth = Math.min(10, scaleX * 0.15);
        const candleCenterX = x + (scaleX * 0.1);
        const footprintStartX = candleCenterX + (candleWidth / 2) + 4;
        const boxWidth = (scaleX * 0.8 - candleWidth - 8) / 2;
        const boxHeight = scaleY * tickSize;
        
        const isBullish = candle.close >= candle.open;
        const color = isBullish ? THEME.green : THEME.red;
        
        // Draw stem
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(Math.round(candleCenterX) + 0.5, Math.round(highY));
        ctx.lineTo(Math.round(candleCenterX) + 0.5, Math.round(lowY));
        ctx.stroke();
        
        // Draw candlestick body
        ctx.fillStyle = color;
        const bodyTop = Math.min(openY, closeY);
        const bodyHeight = Math.max(1, Math.abs(closeY - openY));
        ctx.fillRect(Math.round(candleCenterX - candleWidth/2), Math.round(bodyTop), candleWidth, bodyHeight);

        // Footprint Logic
        const fontSize = Math.max(8, Math.min(12, boxHeight * 0.7));
        ctx.font = `${fontSize}px -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        
        const fpKeys = Object.keys(candle.footprint).sort((a,b) => parseFloat(b) - parseFloat(a));
        
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
        
        fpKeys.forEach(priceStr => {
            const price = parseFloat(priceStr);
            const vols = candle.footprint[priceStr];
            const pY = chartHeight - (price * scaleY + offsetY) - boxHeight/2;
            
            if (pY + boxHeight < 0 || pY > chartHeight) return;
            
            const isPoc = (price === pocPrice);
            
            // BID (Left column)
            const bidStr = formatVol(vols.bid);
            const bidIntensity = Math.min(1.0, vols.bid / maxVol);
            
            if (isPoc) {
                ctx.fillStyle = THEME.pocBg;
            } else if (bidIntensity > 0.3) {
                ctx.fillStyle = THEME.red;
            } else if (vols.bid > 0) {
                ctx.fillStyle = THEME.footprintBgRed;
            } else {
                ctx.fillStyle = 'transparent';
            }
            if (vols.bid > 0 || isPoc) {
                ctx.fillRect(footprintStartX, pY, boxWidth, boxHeight);
                ctx.fillStyle = (isPoc || bidIntensity > 0.3) ? THEME.pocText : THEME.text;
                if (bidStr) ctx.fillText(bidStr, footprintStartX + boxWidth/2, pY + boxHeight/2);
            }
            
            // ASK (Right column)
            const askStr = formatVol(vols.ask);
            const askIntensity = Math.min(1.0, vols.ask / maxVol);
            
            if (isPoc) {
                ctx.fillStyle = THEME.pocBg;
            } else if (askIntensity > 0.3) {
                ctx.fillStyle = THEME.green;
            } else if (vols.ask > 0) {
                ctx.fillStyle = THEME.footprintBgGreen;
            } else {
                ctx.fillStyle = 'transparent';
            }
            if (vols.ask > 0 || isPoc) {
                ctx.fillRect(footprintStartX + boxWidth, pY, boxWidth, boxHeight);
                ctx.fillStyle = (isPoc || askIntensity > 0.3) ? THEME.pocText : THEME.text;
                if (askStr) ctx.fillText(askStr, footprintStartX + boxWidth + boxWidth/2, pY + boxHeight/2);
            }
            
            // Draw separator between bid and ask
            ctx.fillStyle = 'rgba(0,0,0,0.05)';
            ctx.fillRect(footprintStartX + boxWidth - 1, pY, 2, boxHeight);
            
            // White border if POC
            if (isPoc) {
                ctx.strokeStyle = THEME.textMuted;
                ctx.lineWidth = 1;
                ctx.strokeRect(footprintStartX, pY, boxWidth * 2, boxHeight);
            }
        });
        
        // Draw Delta and Total Volume at bottom of the bar
        if (scaleX > 60 && fpKeys.length > 0) {
            const lowestPrice = parseFloat(fpKeys[fpKeys.length - 1]);
            const pY = chartHeight - (lowestPrice * scaleY + offsetY) - boxHeight/2;
            const bottomY = pY + boxHeight + 15;
            
            if (bottomY < chartHeight - 30 && bottomY > 0) {
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
    ctx.restore();
    
    // Draw static axes
    drawAxes(chartWidth, chartHeight, minPrice, maxPrice, step, candles);
    
    // Highlight Last Traded Price
    if (lastTradedPrice > 0) {
        const ltpY = chartHeight - (lastTradedPrice * scaleY + offsetY);
        if (ltpY >= 0 && ltpY <= chartHeight) {
            ctx.setLineDash([2, 2]);
            ctx.strokeStyle = THEME.red; // TV usually uses red/green based on last tick, we'll use a standard color
            ctx.beginPath();
            ctx.moveTo(0, Math.round(ltpY) + 0.5);
            ctx.lineTo(chartWidth, Math.round(ltpY) + 0.5);
            ctx.stroke();
            ctx.setLineDash([]);
            
            // LTP Label background
            ctx.fillStyle = THEME.red;
            ctx.fillRect(chartWidth, ltpY - 10, AXIS_RIGHT, 21);
            ctx.fillStyle = '#FFF';
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.font = '12px -apple-system';
            ctx.fillText(formatPrice(lastTradedPrice), chartWidth + 6, ltpY);
        }
    }
    
    // Crosshair rendering
    if (mouseHover && mouseX >= 0 && mouseX <= chartWidth && mouseY >= 0 && mouseY <= chartHeight && !isDragging) {
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = THEME.textMuted;
        ctx.lineWidth = 1;
        
        ctx.beginPath();
        ctx.moveTo(mouseX + 0.5, 0);
        ctx.lineTo(mouseX + 0.5, chartHeight);
        ctx.stroke();
        
        ctx.beginPath();
        ctx.moveTo(0, mouseY + 0.5);
        ctx.lineTo(chartWidth, mouseY + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        
        // Price Label on Y Axis
        const priceAtCursor = (chartHeight - mouseY - offsetY) / scaleY;
        ctx.fillStyle = THEME.pocBg;
        ctx.fillRect(chartWidth, mouseY - 10, AXIS_RIGHT, 21);
        ctx.fillStyle = '#FFF';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(formatPrice(priceAtCursor), chartWidth + 6, mouseY);
        
        // Time Label on X Axis
        const timeIndex = Math.floor((mouseX - offsetX) / scaleX);
        let timeLabel = '-';
        if (timeIndex >= 0 && timeIndex < candles.length) {
            timeLabel = formatTime(candles[timeIndex].timestamp);
        }
        
        const lblWidth = ctx.measureText(timeLabel).width + 16;
        ctx.fillStyle = THEME.pocBg;
        ctx.fillRect(mouseX - lblWidth/2, chartHeight, lblWidth, AXIS_BOTTOM);
        ctx.fillStyle = '#FFF';
        ctx.textAlign = 'center';
        ctx.fillText(timeLabel, mouseX, chartHeight + AXIS_BOTTOM/2);
    }
}
