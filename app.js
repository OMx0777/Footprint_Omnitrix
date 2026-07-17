const chartContainer = document.getElementById('chart-container');
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
let tickSize = 0.01;

// THEME setup
let THEME = {
    bg: '#ffffff', grid: '#e0e3eb', text: '#131722',
    green: '#089981', red: '#f23645', blue: '#2962FF',
    volProfile: 'rgba(41, 98, 255, 0.15)',
    footprintBgGreen: 'rgba(8, 153, 129, 0.15)', footprintBgRed: 'rgba(242, 54, 69, 0.15)',
    textMuted: '#787b86', pocBg: '#131722', pocText: '#ffffff'
};

function updateTheme() {
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
    
    if (chart) {
        chart.applyOptions({
            layout: { background: { color: THEME.bg }, textColor: THEME.text },
            grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
            rightPriceScale: { borderColor: THEME.grid },
            timeScale: { borderColor: THEME.grid }
        });
    }
}

if (themeToggle) {
    themeToggle.addEventListener('click', () => {
        document.body.classList.toggle('dark-theme');
        updateTheme();
        drawOverlay();
    });
}

// ----------------------------------------------------
// LIGHTWEIGHT CHARTS INITIALIZATION
// ----------------------------------------------------
const chart = LightweightCharts.createChart(chartContainer, {
    layout: { background: { type: 'solid', color: THEME.bg }, textColor: THEME.text },
    grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
    rightPriceScale: { borderColor: THEME.grid, autoScale: true },
    timeScale: { borderColor: THEME.grid, timeVisible: true, secondsVisible: false, barSpacing: 100 },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
});

const candleSeries = chart.addCandlestickSeries({
    upColor: THEME.green, downColor: THEME.red, borderVisible: false,
    wickUpColor: THEME.green, wickDownColor: THEME.red
});

// Create Overlay Canvas
const overlayCanvas = document.createElement('canvas');
overlayCanvas.style.position = 'absolute';
overlayCanvas.style.top = '0';
overlayCanvas.style.left = '0';
overlayCanvas.style.pointerEvents = 'none';
overlayCanvas.style.zIndex = '5';
chartContainer.appendChild(overlayCanvas);
const ctx = overlayCanvas.getContext('2d');

function syncCanvasSize() {
    // Assuming LWC axes are roughly 65px right and 26px bottom in v4
    const rightAxisW = 65; 
    const bottomAxisH = 26;
    overlayCanvas.width = chartContainer.clientWidth - rightAxisW;
    overlayCanvas.height = chartContainer.clientHeight - bottomAxisH;
    drawOverlay();
}
window.addEventListener('resize', () => {
    chart.resize(chartContainer.clientWidth, chartContainer.clientHeight);
    syncCanvasSize();
});
setTimeout(syncCanvasSize, 100);

// Redraw overlay on any chart movement
chart.timeScale().subscribeVisibleTimeRangeChange(drawOverlay);
chart.timeScale().subscribeVisibleLogicalRangeChange(drawOverlay);

chart.subscribeClick((param) => {
    if (isSelectingAnchor && param.time) {
        anchorTimestamp = param.time;
        isSelectingAnchor = false;
        chartContainer.style.cursor = 'default';
        drawOverlay();
    }
});

chart.subscribeCrosshairMove((param) => {
    if (isSelectingAnchor) chartContainer.style.cursor = 'crosshair';
    else chartContainer.style.cursor = 'default';
});

// ----------------------------------------------------
// MODE SWITCHING & DATA PIPELINE
// ----------------------------------------------------
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
        drawOverlay();
    });
});

timeframeSelect.addEventListener('change', (e) => {
    currentTimeframe = parseInt(e.target.value);
    updateChartData();
});

symbolSelect.addEventListener('change', (e) => {
    currentSymbol = e.target.value;
    updateChartData();
});

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
                if (symbolData.candles.length > 600) symbolData.candles.shift(); // Keep more history for LWC
            }
            
            if (msg.symbol === currentSymbol) {
                // Determine step dynamically based on price range
                if (msg.candle.close > 1000) tickSize = 0.25;
                else if (msg.candle.close > 100) tickSize = 0.05;
                else tickSize = 0.01;
                
                updateChartData(msg.candle);
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
        opt.value = sym; opt.textContent = sym;
        symbolSelect.appendChild(opt);
    });
    if (symbols.includes(currentValue)) {
        symbolSelect.value = currentValue;
    } else {
        symbolSelect.value = symbols[0];
        currentSymbol = symbols[0];
        updateChartData();
    }
}

let aggregatedDisplayCandles = [];

function updateChartData(liveCandle = null) {
    if (!currentSymbol || !footprintData[currentSymbol]) return;
    
    if (liveCandle && currentTimeframe === 1) {
        // Fast path for 1m updates
        const mapped = {
            time: liveCandle.timestamp,
            open: liveCandle.open, high: liveCandle.high,
            low: liveCandle.low, close: liveCandle.close,
            footprint: liveCandle.footprint
        };
        candleSeries.update(mapped);
        
        let found = false;
        for (let i = aggregatedDisplayCandles.length - 1; i >= 0; i--) {
            if (aggregatedDisplayCandles[i].time === mapped.time) {
                aggregatedDisplayCandles[i] = mapped;
                found = true; break;
            }
        }
        if (!found) aggregatedDisplayCandles.push(mapped);
        
    } else {
        const baseCandles = footprintData[currentSymbol].candles;
        aggregatedDisplayCandles = getDisplayCandles(baseCandles, currentTimeframe);
        candleSeries.setData(aggregatedDisplayCandles);
    }
    
    drawOverlay();
}

function getDisplayCandles(baseCandles, timeframeMinutes) {
    if (baseCandles.length === 0) return [];
    const aggregated = [];
    let currentAgg = null;
    const intervalSec = timeframeMinutes * 60;
    for (let c of baseCandles) {
        const bucketTs = Math.floor(c.timestamp / intervalSec) * intervalSec;
        if (!currentAgg || currentAgg.time !== bucketTs) {
            if (currentAgg) aggregated.push(currentAgg);
            currentAgg = { time: bucketTs, open: c.open, high: c.high, low: c.low, close: c.close, footprint: {} };
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

function formatVol(v) {
    if (v === 0) return "";
    if (v >= 1000) return (v / 1000).toFixed(2) + "K";
    return v.toString();
}

// ----------------------------------------------------
// OVERLAY RENDERERS
// ----------------------------------------------------

function drawOverlay() {
    ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
    if (!aggregatedDisplayCandles || aggregatedDisplayCandles.length === 0) return;
    
    // Hide native candles if we are drawing footprints or heatmap
    if (currentMode === 'heatmap') {
        candleSeries.applyOptions({ visible: false });
        drawHeatmap();
    } else {
        candleSeries.applyOptions({ visible: true });
        
        let maxVol = 1;
        aggregatedDisplayCandles.forEach(c => {
            Object.values(c.footprint).forEach(fp => {
                if (fp.bid + fp.ask > maxVol) maxVol = fp.bid + fp.ask;
            });
        });
        
        const logicalRange = chart.timeScale().getVisibleLogicalRange();
        if (!logicalRange) return;
        
        // Calculate dynamic bar width
        const x1 = chart.timeScale().logicalToCoordinate(Math.max(0, Math.floor(logicalRange.from)));
        const x2 = chart.timeScale().logicalToCoordinate(Math.max(0, Math.floor(logicalRange.from) + 1));
        const scaleX = x2 && x1 ? Math.abs(x2 - x1) : 100;
        
        // Determine box height dynamically based on price scale
        const y1 = candleSeries.priceToCoordinate(100);
        const y2 = candleSeries.priceToCoordinate(100 - tickSize);
        let boxHeight = y2 && y1 ? Math.abs(y2 - y1) : 12;

        aggregatedDisplayCandles.forEach((candle, i) => {
            if (i < Math.floor(logicalRange.from) - 1 || i > Math.ceil(logicalRange.to) + 1) return;
            const x = chart.timeScale().coordinateToTime ? chart.timeScale().timeToCoordinate(candle.time) : chart.timeScale().logicalToCoordinate(i);
            if (x === null) return;
            
            if (currentMode === 'footprint') {
                drawFootprintData(candle, x, scaleX, boxHeight);
            } else if (currentMode === 'cluster') {
                drawClusterData(candle, x, scaleX, boxHeight, maxVol);
            }
        });
        
        if (currentMode === 'vp-fixed') drawVolumeProfile(false, null, boxHeight);
        else if (currentMode === 'vp-anchored') drawVolumeProfile(true, anchorTimestamp, boxHeight);
    }
}

function drawFootprintData(candle, x, scaleX, boxHeight) {
    if (scaleX < 30) return; // Hide text if too zoomed out
    
    const fpKeys = Object.keys(candle.footprint).sort((a,b) => parseFloat(b) - parseFloat(a));
    const candleWidth = Math.min(10, scaleX * 0.15);
    const footprintStartX = x + (candleWidth / 2) + 4;
    const boxWidth = (scaleX * 0.8 - candleWidth - 8) / 2;
    
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
        const pY = candleSeries.priceToCoordinate(price) - boxHeight/2;
        if (pY === null || pY + boxHeight < 0 || pY > overlayCanvas.height) return;
        
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
        const pY = candleSeries.priceToCoordinate(lowestPrice) - boxHeight/2;
        const bottomY = pY + boxHeight + 15;
        if (bottomY < overlayCanvas.height) {
            ctx.fillStyle = THEME.text;
            ctx.font = '11px -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif';
            ctx.textAlign = 'right';
            ctx.fillText("Delta", x - 4, bottomY);
            ctx.fillText("Total", x - 4, bottomY + 14);
            ctx.textAlign = 'left';
            ctx.fillStyle = totalDelta >= 0 ? THEME.green : THEME.red;
            ctx.fillText((totalDelta > 0 ? "+" : "") + formatVol(totalDelta), x + 4, bottomY);
            ctx.fillStyle = THEME.text;
            ctx.font = 'bold 11px -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif';
            ctx.fillText(formatVol(totalVolBar), x + 4, bottomY + 14);
        }
    }
}

function drawClusterData(candle, x, scaleX, boxHeight, maxVol) {
    if (scaleX < 15) return;
    
    Object.keys(candle.footprint).forEach(priceStr => {
        const price = parseFloat(priceStr);
        const vols = candle.footprint[priceStr];
        const pY = candleSeries.priceToCoordinate(price);
        if (pY === null || pY + boxHeight < 0 || pY - boxHeight > overlayCanvas.height) return;
        
        const total = vols.bid + vols.ask;
        if (total === 0) return;
        const delta = vols.ask - vols.bid;
        
        const maxRadius = Math.max(2, (scaleX * 0.6) / 2);
        const radius = Math.max(2, Math.min(maxRadius, (total / maxVol) * maxRadius));
        
        ctx.beginPath();
        ctx.arc(x, pY, radius, 0, 2 * Math.PI);
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

function drawHeatmap() {
    let absoluteMaxVol = 1;
    aggregatedDisplayCandles.forEach(c => {
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

    const logicalRange = chart.timeScale().getVisibleLogicalRange();
    if (!logicalRange) return;
    
    const x1 = chart.timeScale().logicalToCoordinate(Math.max(0, Math.floor(logicalRange.from)));
    const x2 = chart.timeScale().logicalToCoordinate(Math.max(0, Math.floor(logicalRange.from) + 1));
    const scaleX = x2 && x1 ? Math.abs(x2 - x1) : 10;
    
    const y1 = candleSeries.priceToCoordinate(100);
    const y2 = candleSeries.priceToCoordinate(100 - tickSize);
    let boxHeight = y2 && y1 ? Math.abs(y2 - y1) : 12;

    for (let i = Math.floor(logicalRange.from); i <= Math.ceil(logicalRange.to); i++) {
        const candle = aggregatedDisplayCandles[i];
        if (!candle) continue;
        const x = chart.timeScale().logicalToCoordinate(i);
        if (x === null) continue;
        
        Object.keys(candle.footprint).forEach(priceStr => {
            const price = parseFloat(priceStr);
            const vols = candle.footprint[priceStr];
            const pY = candleSeries.priceToCoordinate(price) - boxHeight/2;
            if (pY === null || pY + boxHeight < 0 || pY > overlayCanvas.height) return;
            const total = vols.bid + vols.ask;
            if (total === 0) return;
            
            ctx.fillStyle = getHeatColor(total, absoluteMaxVol);
            ctx.fillRect(x - scaleX/2, Math.floor(pY), scaleX, Math.ceil(boxHeight) + 1);
        });
    }
}

function drawVolumeProfile(isAnchored, anchorTs, boxHeight) {
    const vp = {};
    let maxVpVol = 0;
    let totalVPVol = 0;
    
    aggregatedDisplayCandles.forEach(c => {
        if (isAnchored && anchorTs && c.time < anchorTs) return;
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
    
    const vpWidth = overlayCanvas.width * 0.25; 
    
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
        const pY = candleSeries.priceToCoordinate(price) - boxHeight/2;
        if (pY === null || pY + boxHeight < 0 || pY > overlayCanvas.height) return;
        
        const data = vp[priceStr];
        const barW = (data.total / maxVpVol) * vpWidth;
        const bidW = (data.bid / data.total) * barW;
        const askW = (data.ask / data.total) * barW;
        
        const startX = overlayCanvas.width - barW;
        
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
        const ax = chart.timeScale().timeToCoordinate(anchorTs);
        if (ax !== null) {
            ctx.setLineDash([5, 5]);
            ctx.strokeStyle = THEME.highlight;
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(ax, 0); ctx.lineTo(ax, overlayCanvas.height);
            ctx.stroke();
            ctx.setLineDash([]);
        }
    }
}
